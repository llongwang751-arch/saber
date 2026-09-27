"""Transactional SQLite adapter for runs; independent of Agents and HTTP.

Every state/event mutation holds a database write transaction, including
migration and orphan reconciliation. Completed runs are immutable.
"""

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from internal.observability import sanitize_trace
from .plan_contracts import validate_research_plan
from .run_contracts import TERMINAL, RunCapacityExceeded, RunConflict, RunNotFound


class SQLiteRunRepository:
    def __init__(self, path, *, max_active=64, stale_after=10.0):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.max_active, self.stale_after = max_active, stale_after
        self._db.execute("PRAGMA busy_timeout=30000")
        deadline = time.monotonic() + 30
        while True:
            try:
                self._db.execute("PRAGMA journal_mode=WAL")
                break
            except sqlite3.OperationalError as exc:
                code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
                if code not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} or time.monotonic() >= deadline:
                    self._db.close()
                    raise
                time.sleep(0.05)
        self._db.execute("PRAGMA foreign_keys=ON")
        with self.transaction():
            self._db.execute("""CREATE TABLE IF NOT EXISTS agent_runs (
                run_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
                message TEXT NOT NULL, use_rag INTEGER NOT NULL, status TEXT NOT NULL,
                result_json TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                worker_id TEXT, request_key TEXT, kind TEXT NOT NULL DEFAULT 'chat',
                task_id TEXT NOT NULL DEFAULT '')""")
            columns = {r["name"] for r in self._db.execute("PRAGMA table_info(agent_runs)")}
            for name, definition in {
                "worker_id": "TEXT",
                "request_key": "TEXT",
                "kind": "TEXT NOT NULL DEFAULT 'chat'",
                "task_id": "TEXT NOT NULL DEFAULT ''",
                "plan_json": "TEXT",
                "plan_version": "INTEGER NOT NULL DEFAULT 0",
                "plan_status": "TEXT NOT NULL DEFAULT ''",
                "research_state_json": "TEXT",
                "parent_run_id": "TEXT NOT NULL DEFAULT ''",
            }.items():
                if name not in columns:
                    self._db.execute(f"ALTER TABLE agent_runs ADD COLUMN {name} {definition}")
            self._db.execute("""CREATE TABLE IF NOT EXISTS agent_run_workers (
                worker_id TEXT PRIMARY KEY, heartbeat_at REAL NOT NULL)""")
            self._db.execute("""CREATE TABLE IF NOT EXISTS agent_run_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                owner_id TEXT NOT NULL, event_type TEXT NOT NULL, data_json TEXT NOT NULL,
                created_at REAL NOT NULL, FOREIGN KEY(run_id) REFERENCES agent_runs(run_id))""")
            self._db.execute("CREATE INDEX IF NOT EXISTS idx_agent_runs_owner ON agent_runs(owner_id, created_at DESC)")
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_run_events_run ON agent_run_events(run_id, event_id)"
            )
            self._db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_runs_request_key
                ON agent_runs(owner_id, request_key) WHERE request_key IS NOT NULL""")
            self._db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_runs_continuation
                ON agent_runs(owner_id, parent_run_id) WHERE parent_run_id<>''""")
            # The original index remains valid for executing work. A second index
            # also reserves a waiting plan's conversation without retaining a worker.
            self._db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_runs_active_conversation
                ON agent_runs(owner_id, conversation_id) WHERE status IN ('pending','running','cancelling')""")
            self._db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_runs_open_conversation
                ON agent_runs(owner_id, conversation_id)
                WHERE status IN ('pending','running','cancelling','awaiting_plan_review')""")

    @contextmanager
    def transaction(self):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise

    @staticmethod
    def _encode(data, terminal=False):
        encoded = json.dumps(data if terminal else sanitize_trace(data), ensure_ascii=False, default=str)
        if not terminal and len(encoded) > 65536:
            encoded = json.dumps({"truncated": True, "preview": encoded[:4000]}, ensure_ascii=False)
        return encoded

    def _append(self, run_id, owner_id, event_type, data):
        return int(
            self._db.execute(
                "INSERT INTO agent_run_events(run_id,owner_id,event_type,data_json,created_at) VALUES (?,?,?,?,?)",
                (run_id, owner_id, event_type, self._encode(data, terminal=event_type == "done"), time.time()),
            ).lastrowid
        )

    def _finish(self, run_id, owner_id, status, payload):
        changed = self._db.execute(
            """UPDATE agent_runs SET status=?, result_json=?, updated_at=?
            WHERE run_id=? AND owner_id=? AND status IN ('pending','running','cancelling','awaiting_plan_review')""",
            (status, self._encode(payload, terminal=True), time.time(), run_id, owner_id),
        ).rowcount
        if changed:
            self._append(run_id, owner_id, "done", payload)

    def register_worker(self, worker_id):
        with self.transaction():
            self._db.execute("INSERT INTO agent_run_workers VALUES (?,?)", (worker_id, time.time()))
            self._reconcile()

    def _reconcile(self):
        cutoff = time.time() - self.stale_after
        rows = self._db.execute(
            """SELECT r.run_id,r.owner_id FROM agent_runs r
            LEFT JOIN agent_run_workers w ON w.worker_id=r.worker_id
            WHERE r.status IN ('pending','running','cancelling')
            AND (r.worker_id IS NULL OR w.heartbeat_at IS NULL OR w.heartbeat_at<?)""",
            (cutoff,),
        ).fetchall()
        for row in rows:
            self._finish(
                row["run_id"],
                row["owner_id"],
                "interrupted",
                {"status": "interrupted", "reason": "worker_lost; inspect tool checkpoints before retry"},
            )
        self._db.execute("DELETE FROM agent_run_workers WHERE heartbeat_at<?", (cutoff,))

    def heartbeat(self, worker_id):
        with self.transaction():
            alive = bool(
                self._db.execute(
                    "UPDATE agent_run_workers SET heartbeat_at=? WHERE worker_id=? AND heartbeat_at>=?",
                    (time.time(), worker_id, time.time() - self.stale_after),
                ).rowcount
            )
            self._reconcile()
            cancellations = self._db.execute(
                "SELECT run_id,owner_id FROM agent_runs WHERE worker_id=? AND status='cancelling'",
                (worker_id,),
            ).fetchall()
            return alive, [dict(row) for row in cancellations]

    def release_worker(self, worker_id):
        with self.transaction():
            rows = self._db.execute(
                "SELECT run_id,owner_id FROM agent_runs WHERE worker_id=? AND status IN ('pending','running','cancelling')",
                (worker_id,),
            ).fetchall()
            for row in rows:
                self._finish(
                    row["run_id"],
                    row["owner_id"],
                    "interrupted",
                    {"status": "interrupted", "reason": "worker_shutdown; inspect tool checkpoints before retry"},
                )
            self._db.execute("DELETE FROM agent_run_workers WHERE worker_id=?", (worker_id,))

    def reserve(self, owner_id, worker_id, message, conversation_id, use_rag, request_key, kind, task_id, *, resume_from=""):
        requested_conversation = conversation_id
        conversation_id = conversation_id or str(uuid.uuid4())
        run_id, now = str(uuid.uuid4()), time.time()
        try:
            with self.transaction():
                if request_key:
                    row = self._db.execute(
                        "SELECT * FROM agent_runs WHERE owner_id=? AND request_key=?", (owner_id, request_key)
                    ).fetchone()
                    if row is not None:
                        if (
                            row["message"] != message
                            or bool(row["use_rag"]) != use_rag
                            or row["kind"] != kind
                            or row["task_id"] != task_id
                            or row["parent_run_id"] != resume_from
                            or (requested_conversation and row["conversation_id"] != requested_conversation)
                        ):
                            raise RunConflict("Idempotency key was already used for a different request")
                        return self._row(row), False
                parent = None
                if resume_from:
                    parent = self._owned(owner_id, resume_from)
                    if parent["kind"] != "research" or parent["status"] != "interrupted" or parent["plan_status"] != "approved":
                        raise RunConflict("Only an interrupted, approved research run can be resumed")
                    continuation = self._db.execute(
                        "SELECT * FROM agent_runs WHERE owner_id=? AND parent_run_id=?", (owner_id, resume_from),
                    ).fetchone()
                    if continuation is not None:
                        return self._row(continuation), False
                if not self._db.execute(
                    "SELECT 1 FROM agent_run_workers WHERE worker_id=? AND heartbeat_at>=?",
                    (worker_id, time.time() - self.stale_after),
                ).fetchone():
                    raise RunConflict("Run worker lease has expired")
                active = self._db.execute(
                    "SELECT COUNT(*) FROM agent_runs WHERE status IN ('pending','running','cancelling')"
                ).fetchone()[0]
                if active >= self.max_active:
                    raise RunCapacityExceeded("Run queue is full; retry after an active run finishes")
                self._db.execute(
                    """INSERT INTO agent_runs
                    (run_id,owner_id,conversation_id,message,use_rag,status,created_at,updated_at,worker_id,request_key,kind,task_id)
                    VALUES (?,?,?,?,?,'pending',?,?,?,?,?,?)""",
                    (
                        run_id,
                        owner_id,
                        conversation_id,
                        message,
                        int(use_rag),
                        now,
                        now,
                        worker_id,
                        request_key or None,
                        kind,
                        task_id,
                    ),
                )
                self._append(run_id, owner_id, "queued", {"conversation_id": conversation_id})
                if parent is not None:
                    self._db.execute(
                        """UPDATE agent_runs SET parent_run_id=?,plan_json=?,plan_version=?,plan_status='approved',
                        research_state_json=? WHERE run_id=?""",
                        (resume_from, parent["plan_json"], parent["plan_version"], parent["research_state_json"], run_id),
                    )
                    self._append(run_id, owner_id, "research_resumed", {
                        "parent_run_id": resume_from, "version": parent["plan_version"],
                    })
        except sqlite3.IntegrityError as exc:
            raise RunConflict("This conversation already has an active run") from exc
        return self.get(owner_id, run_id), True

    def _worker_owns(self, row, worker_id):
        return row["worker_id"] == worker_id and self._db.execute(
            "SELECT 1 FROM agent_run_workers WHERE worker_id=? AND heartbeat_at>=?",
            (worker_id, time.time() - self.stale_after),
        ).fetchone() is not None

    def start(self, owner_id, run_id, *, worker_id=None):
        with self.transaction():
            row = self._owned(owner_id, run_id)
            if worker_id is not None and not self._worker_owns(row, worker_id):
                return False
            if row["status"] == "cancelling":
                self._finish(run_id, owner_id, "cancelled", {"status": "cancelled"})
                return False
            if row["status"] != "pending":
                return False
            live = self._db.execute(
                "SELECT 1 FROM agent_run_workers WHERE worker_id=? AND heartbeat_at>=?",
                (row["worker_id"], time.time() - self.stale_after),
            ).fetchone()
            if not live:
                self._finish(run_id, owner_id, "interrupted", {"status": "interrupted", "reason": "worker_lost"})
                return False
            self._db.execute(
                "UPDATE agent_runs SET status='running',updated_at=? WHERE run_id=?", (time.time(), run_id)
            )
            self._append(run_id, owner_id, "started", {"conversation_id": row["conversation_id"]})
            return True

    def append_event(self, run_id, owner_id, event_type, data, *, worker_id=None):
        with self.transaction():
            row = self._owned(owner_id, run_id)
            if row["status"] in TERMINAL:
                return 0
            if worker_id is not None and not self._worker_owns(row, worker_id):
                return 0
            return self._append(run_id, owner_id, event_type, data)

    def finish(self, run_id, owner_id, status, payload, *, worker_id=None):
        if status not in TERMINAL:
            raise ValueError("Expected a terminal run status")
        with self.transaction():
            row = self._owned(owner_id, run_id)
            if worker_id is not None and not self._worker_owns(row, worker_id):
                return
            if row["status"] == "cancelling":
                status, payload = "cancelled", {**payload, "status": "cancelled"}
            self._finish(run_id, owner_id, status, payload)

    def pause_for_plan(self, owner_id, run_id, worker_id, plan):
        plan = validate_research_plan(plan)
        with self.transaction():
            row = self._owned(owner_id, run_id)
            if not self._worker_owns(row, worker_id):
                raise RunConflict("Research worker lease expired")
            if row["status"] == "cancelling":
                self._finish(run_id, owner_id, "cancelled", {"status": "cancelled"})
                return False
            if row["kind"] != "research" or row["status"] != "running" or row["plan_version"]:
                raise RunConflict("Run cannot accept a new research plan")
            self._db.execute(
                """UPDATE agent_runs SET status='awaiting_plan_review', worker_id=NULL,
                plan_json=?,plan_version=1,plan_status='pending',updated_at=? WHERE run_id=?""",
                (self._encode(plan, terminal=True), time.time(), run_id),
            )
            data = {"plan": plan, "version": 1, "status": "awaiting_plan_review"}
            self._append(run_id, owner_id, "plan_created", data)
            self._append(run_id, owner_id, "plan_review_required", {
                "version": 1, "status": "awaiting_plan_review",
            })
            return True

    def get_plan(self, owner_id, run_id):
        with self._lock:
            row = self._owned(owner_id, run_id)
            if row["kind"] != "research":
                raise RunConflict("Run is not a research run")
            return {
                "run_id": run_id, "status": row["status"],
                "plan": json.loads(row["plan_json"]) if row["plan_json"] else None,
                "version": row["plan_version"], "review_status": row["plan_status"],
            }

    def review_plan(self, owner_id, run_id, *, action, version, worker_id=None, plan=None, steps=None):
        if action not in {"approve", "edit", "reject"}:
            raise ValueError("Unknown plan review action")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise ValueError("A positive plan version is required")
        if action != "edit" and (plan is not None or steps is not None):
            raise ValueError("Only edit accepts plan changes")
        if plan is not None and steps is not None:
            raise ValueError("Provide either plan or steps, not both")
        with self.transaction():
            row = self._owned(owner_id, run_id)
            if row["status"] != "awaiting_plan_review" or row["plan_version"] != version:
                raise RunConflict("Plan changed or is no longer awaiting review; reload before reviewing")
            now = time.time()
            if action == "edit":
                if plan is None and steps is None:
                    raise ValueError("An edited plan or steps is required")
                candidate = plan if plan is not None else {**json.loads(row["plan_json"]), "steps": steps}
                candidate = validate_research_plan(candidate)
                self._db.execute(
                    "UPDATE agent_runs SET plan_json=?,plan_version=plan_version+1,updated_at=? WHERE run_id=?",
                    (self._encode(candidate, terminal=True), now, run_id),
                )
                self._append(run_id, owner_id, "plan_edited", {
                    "plan": candidate, "version": version + 1, "status": "awaiting_plan_review",
                })
            elif action == "reject":
                self._db.execute("UPDATE agent_runs SET plan_status='rejected' WHERE run_id=?", (run_id,))
                self._append(run_id, owner_id, "plan_rejected", {"version": version, "status": "cancelled"})
                self._finish(run_id, owner_id, "cancelled", {"status": "cancelled", "reason": "plan_rejected"})
            else:
                if not self.worker_healthy(worker_id):
                    raise RunConflict("Run worker lease has expired")
                active = self._db.execute(
                    "SELECT COUNT(*) FROM agent_runs WHERE status IN ('pending','running','cancelling')"
                ).fetchone()[0]
                if active >= self.max_active:
                    raise RunCapacityExceeded("Run queue is full; retry approval when a worker is available")
                self._db.execute(
                    """UPDATE agent_runs SET status='pending',plan_status='approved',worker_id=?,updated_at=?
                    WHERE run_id=?""", (worker_id, now, run_id),
                )
                self._append(run_id, owner_id, "plan_approved", {"version": version, "status": "pending"})
            # Capture a consistent review response while still in this transaction.
            return self.get_plan(owner_id, run_id)

    def checkpoint_research(self, owner_id, run_id, worker_id, state):
        if not isinstance(state, dict):
            raise ValueError("Research checkpoint must be an object")
        encoded = self._encode(state, terminal=True)
        if len(encoded) > 8 * 1024 * 1024:
            raise ValueError("Research checkpoint exceeds 8 MiB")
        with self.transaction():
            row = self._owned(owner_id, run_id)
            if row["status"] not in {"running", "cancelling"} or not self._worker_owns(row, worker_id):
                raise RunConflict("Research execution no longer owns the run")
            self._db.execute(
                "UPDATE agent_runs SET research_state_json=?,updated_at=? WHERE run_id=?",
                (encoded, time.time(), run_id),
            )

    def request_cancel(self, owner_id, run_id):
        with self.transaction():
            row = self._owned(owner_id, run_id)
            if row["status"] == "awaiting_plan_review":
                self._append(run_id, owner_id, "cancel_requested", {})
                self._finish(run_id, owner_id, "cancelled", {"status": "cancelled"})
            if row["status"] in {"pending", "running"}:
                self._db.execute(
                    "UPDATE agent_runs SET status='cancelling',updated_at=? WHERE run_id=?", (time.time(), run_id)
                )
                self._append(run_id, owner_id, "cancel_requested", {})
        return self.get(owner_id, run_id)

    def _owned(self, owner_id, run_id):
        row = self._db.execute("SELECT * FROM agent_runs WHERE owner_id=? AND run_id=?", (owner_id, run_id)).fetchone()
        if row is None:
            raise RunNotFound(run_id)
        return row

    @staticmethod
    def _row(row):
        state = json.loads(row["research_state_json"]) if row["research_state_json"] else {}
        result = json.loads(row["result_json"]) if row["result_json"] else None
        response = (result or {}).get("response") or {}
        return {
            **{
                key: row[key]
                for key in (
                    "run_id",
                    "conversation_id",
                    "message",
                    "status",
                    "created_at",
                    "updated_at",
                    "kind",
                    "task_id",
                    "parent_run_id",
                )
            },
            "use_rag": bool(row["use_rag"]),
            "result": result,
            "plan": json.loads(row["plan_json"]) if row["plan_json"] else None,
            "plan_version": row["plan_version"],
            "plan_status": row["plan_status"],
            "research_state": state,
            "sources": response.get("sources", state.get("sources", [])),
            "artifacts": response.get("artifacts", state.get("artifacts", [])),
        }

    def get(self, owner_id, run_id):
        with self._lock:
            return self._row(self._owned(owner_id, run_id))

    def list(self, owner_id, *, limit=50):
        with self._lock:
            rows = self._db.execute(
                """SELECT run_id,conversation_id,message,use_rag,status,created_at,updated_at,kind,task_id,
                plan_version,plan_status,parent_run_id
                FROM agent_runs WHERE owner_id=? ORDER BY created_at DESC LIMIT ?""",
                (owner_id, max(1, min(limit, 100))),
            ).fetchall()
            return [{**dict(row), "use_rag": bool(row["use_rag"])} for row in rows]

    def summary(self, owner_id):
        with self._lock:
            counts = {
                r["status"]: r["n"]
                for r in self._db.execute(
                    "SELECT status,COUNT(*) n FROM agent_runs WHERE owner_id=? GROUP BY status", (owner_id,)
                )
            }
            active = self._db.execute(
                "SELECT COUNT(*) FROM agent_runs WHERE status IN ('pending','running','cancelling')"
            ).fetchone()[0]
            workers = self._db.execute(
                "SELECT COUNT(*) FROM agent_run_workers WHERE heartbeat_at>=?", (time.time() - self.stale_after,)
            ).fetchone()[0]
            return {"counts": counts, "active": active, "capacity": self.max_active, "healthy_workers": workers}

    def active_ids(self, owner_id, conversation_id=""):
        with self._lock:
            sql = """SELECT run_id FROM agent_runs WHERE owner_id=?
            AND status IN ('pending','running','cancelling','awaiting_plan_review')"""
            params = [owner_id]
            if conversation_id:
                sql += " AND conversation_id=?"
                params.append(conversation_id)
            return [row["run_id"] for row in self._db.execute(sql, params)]

    def worker_healthy(self, worker_id):
        with self._lock:
            return self._db.execute(
                "SELECT 1 FROM agent_run_workers WHERE worker_id=? AND heartbeat_at>=?",
                (worker_id, time.time() - self.stale_after),
            ).fetchone() is not None

    def execution_allowed(self, owner_id, run_id, worker_id):
        """Read the fence before research calls, including between heartbeats."""
        with self._lock:
            row = self._owned(owner_id, run_id)
            return row["status"] in {"pending", "running"} and self._worker_owns(row, worker_id)

    def events_since(self, owner_id, run_id, after_id=0, *, limit=200):
        with self._lock:
            self._owned(owner_id, run_id)
            rows = self._db.execute(
                """SELECT event_id,event_type,data_json,created_at FROM agent_run_events
                WHERE run_id=? AND owner_id=? AND event_id>? ORDER BY event_id LIMIT ?""",
                (run_id, owner_id, max(0, after_id), max(1, min(limit, 500))),
            ).fetchall()
            return [
                {
                    "event_id": row["event_id"],
                    "type": row["event_type"],
                    "data": json.loads(row["data_json"]),
                    "created_at": row["created_at"],
                }
                for row in rows
            ]
