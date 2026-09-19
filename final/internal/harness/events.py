"""Event Sourcing Engine for Harness Runtime 2.0.

Inspired by deepseek-harness's append-only event stream architecture,
this module implements immutable event logs, trajectory replay, session forking,
and crash-consistent checkpoint resumption.
"""

from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional


class EventType(str, Enum):
    SESSION_START = "session_start"
    USER_INPUT = "user_input"
    REASONING = "reasoning"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    STATE_CHANGE = "state_change"
    CHECKPOINT = "checkpoint"
    REPLAN = "replan"
    ERROR = "error"
    SESSION_END = "session_end"


@dataclass(frozen=True)
class HarnessEvent:
    session_id: str
    event_id: str
    type: EventType
    payload: Dict[str, Any]
    parent_event_id: Optional[str] = None
    step_index: int = 0
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["type"] = self.type.value
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> HarnessEvent:
        event_type = EventType(data["type"])
        return cls(
            session_id=data["session_id"],
            event_id=data["event_id"],
            type=event_type,
            payload=data.get("payload", {}),
            parent_event_id=data.get("parent_event_id"),
            step_index=data.get("step_index", 0),
            timestamp=data.get("timestamp", time.time()),
        )


class BaseEventStream:
    """Abstract interface for immutable event streaming."""

    def append(self, event: HarnessEvent) -> None:
        raise NotImplementedError

    def claim_action(self, session_id: str, invocation_id: str) -> bool:
        """Atomic at-most-once dispatch claim. Claims are never auto-expired."""
        raise NotImplementedError("Event storage cannot safely claim actions")

    def get_events(self, session_id: str) -> List[HarnessEvent]:
        raise NotImplementedError

    def replay(self, session_id: str, stop_at_event_id: Optional[str] = None) -> List[HarnessEvent]:
        """Replay history up to a specific event ID for deterministic audit or debugging."""
        events = self.get_events(session_id)
        if not stop_at_event_id:
            return events
        replayed: List[HarnessEvent] = []
        for ev in events:
            replayed.append(ev)
            if ev.event_id == stop_at_event_id:
                break
        return replayed

    def fork(self, source_session_id: str, fork_at_event_id: str, new_session_id: str) -> List[HarnessEvent]:
        """Fork a new parallel session from an arbitrary historical step (Event Branching)."""
        history = self.replay(source_session_id, stop_at_event_id=fork_at_event_id)
        if not history:
            raise ValueError(f"Fork point {fork_at_event_id} not found in session {source_session_id}")

        forked_events: List[HarnessEvent] = []
        for ev in history:
            forked_ev = HarnessEvent(
                session_id=new_session_id,
                event_id=f"fork_{uuid.uuid4().hex[:8]}",
                type=ev.type,
                payload=dict(ev.payload),
                parent_event_id=ev.parent_event_id,
                step_index=ev.step_index,
                timestamp=time.time(),
            )
            self.append(forked_ev)
            forked_events.append(forked_ev)
        return forked_events

    def get_last_checkpoint(self, session_id: str) -> Optional[HarnessEvent]:
        """Retrieve the latest valid checkpoint event for resume capability."""
        events = self.get_events(session_id)
        for ev in reversed(events):
            if ev.type == EventType.CHECKPOINT:
                return ev
        return None


class MemoryEventStream(BaseEventStream):
    """In-memory thread-safe event stream for fast testing and lightweight runtime."""

    def __init__(self):
        self._events: Dict[str, List[HarnessEvent]] = {}
        self._action_claims = set()
        self._lock = threading.RLock()

    def claim_action(self, session_id, invocation_id):
        with self._lock:
            key = (session_id, invocation_id)
            if key in self._action_claims:
                return False
            self._action_claims.add(key)
            return True

    def append(self, event: HarnessEvent) -> None:
        with self._lock:
            self._events.setdefault(event.session_id, []).append(event)

    def get_events(self, session_id: str) -> List[HarnessEvent]:
        with self._lock:
            return list(self._events.get(session_id, []))


class JSONLEventStream(BaseEventStream):
    """Append-only file-backed event stream (similar to deepseek-harness local storage)."""

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _file_path(self, session_id: str) -> Path:
        # Sanitize session_id for filesystem path
        safe_id = "".join(c for c in session_id if c.isalnum() or c in ("-", "_"))
        return self.directory / f"{safe_id}.events.jsonl"

    def claim_action(self, session_id, invocation_id):
        key = hashlib.sha256(json.dumps([session_id, invocation_id]).encode()).hexdigest()
        try:
            with (self.directory / f"{key}.claim").open("x", encoding="utf8") as handle:
                handle.write("claimed\n")
                handle.flush()
                os.fsync(handle.fileno())
            return True
        except FileExistsError:
            return False

    def append(self, event: HarnessEvent) -> None:
        with self._lock:
            path = self._file_path(event.session_id)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")

    def get_events(self, session_id: str) -> List[HarnessEvent]:
        with self._lock:
            path = self._file_path(session_id)
            if not path.exists():
                return []
            events: List[HarnessEvent] = []
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        events.append(HarnessEvent.from_dict(json.loads(line)))
            return events


class SqliteEventStream(BaseEventStream):
    """SQLite-backed event stream for robust local ACID persistence."""

    def __init__(self, db_path: str = ":memory:"):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.RLock()
        self._init_db()

    def _init_db(self) -> None:
        with self._lock:
            self._conn.execute("CREATE TABLE IF NOT EXISTS harness_action_claims (session_id TEXT, invocation_id TEXT, PRIMARY KEY(session_id, invocation_id))")
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS harness_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    event_id TEXT NOT NULL UNIQUE,
                    parent_event_id TEXT,
                    type TEXT NOT NULL,
                    step_index INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    timestamp REAL NOT NULL
                );
            """)
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_session_events ON harness_events(session_id, id);")
            self._conn.commit()

    def claim_action(self, session_id, invocation_id):
        with self._lock:
            result = self._conn.execute("INSERT OR IGNORE INTO harness_action_claims VALUES (?, ?)", (session_id, invocation_id))
            self._conn.commit()
            return result.rowcount == 1

    def append(self, event: HarnessEvent) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO harness_events (session_id, event_id, parent_event_id, type, step_index, payload, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.session_id,
                    event.event_id,
                    event.parent_event_id,
                    event.type.value,
                    event.step_index,
                    json.dumps(event.payload, ensure_ascii=False),
                    event.timestamp,
                ),
            )
            self._conn.commit()

    def get_events(self, session_id: str) -> List[HarnessEvent]:
        with self._lock:
            cursor = self._conn.execute(
                """
                SELECT session_id, event_id, parent_event_id, type, step_index, payload, timestamp
                FROM harness_events
                WHERE session_id = ?
                ORDER BY id ASC
                """,
                (session_id,),
            )
            rows = cursor.fetchall()
            events: List[HarnessEvent] = []
            for row in rows:
                events.append(HarnessEvent(
                    session_id=row[0],
                    event_id=row[1],
                    parent_event_id=row[2],
                    type=EventType(row[3]),
                    step_index=row[4],
                    payload=json.loads(row[5]),
                    timestamp=row[6],
                ))
            return events
