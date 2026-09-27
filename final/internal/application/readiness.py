"""Opt-in, live dependency readiness; legacy liveness remains unchanged.

One outstanding probe per dependency and a bounded executor prevent a broken
driver from spawning unlimited probes. Responses expose no endpoints or secrets.
"""

import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from types import SimpleNamespace

from internal.fastapi_compat import register_shutdown
from .run_runtime import get_run_service

COMPONENTS = frozenset(
    {"application", "run_store", "postgresql", "elasticsearch", "milvus", "kafka", "neo4j", "docker"}
)


class ReadinessProbe:
    def __init__(self, checks, *, timeout=3.0, cache_seconds=5.0):
        self.checks = dict(checks)
        self.timeout, self.cache_seconds = timeout, cache_seconds
        self._executor = ThreadPoolExecutor(max_workers=max(1, len(checks)), thread_name_prefix="readiness")
        self._lock = threading.Lock()
        self._pending = {}
        self._cached, self._expires, self._closed = None, 0.0, False

    def snapshot(self):
        with self._lock:
            if self._closed:
                return {"ready": False, "enforced": bool(self.checks), "checks": {}, "reason": "shutting_down"}
            if self._cached is not None and time.monotonic() < self._expires:
                return self._cached
            for name, check in self.checks.items():
                if name not in self._pending:
                    self._pending[name] = self._executor.submit(check)
            wait(list(self._pending.values()), timeout=self.timeout)
            results = {}
            for name, future in list(self._pending.items()):
                if not future.done():
                    results[name] = {"ready": False, "reason": "probe_timeout"}
                    continue
                self._pending.pop(name)
                try:
                    success = future.result() is True
                    results[name] = {"ready": success, "reason": "ok" if success else "unavailable"}
                except Exception as exc:
                    results[name] = {"ready": False, "reason": type(exc).__name__}
            self._cached = {
                "ready": bool(results) and all(item["ready"] for item in results.values()),
                "enforced": bool(self.checks),
                "checked_at": time.time(),
                "checks": results,
            }
            self._expires = time.monotonic() + self.cache_seconds
            return self._cached

    def close(self):
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)


def install_readiness(app, agent, inf, cfg):
    required = {name.strip() for name in os.getenv("AGI_READINESS_REQUIRED", "").split(",") if name.strip()}
    if required - COMPONENTS:
        raise ValueError("Unknown readiness dependencies: " + ",".join(sorted(required - COMPONENTS)))

    def application():
        with app.state.application_store.engine.connect() as connection:
            return connection.exec_driver_sql("SELECT 1").scalar() == 1

    def runs():
        return get_run_service(SimpleNamespace(app=app)).healthy()

    def postgres():
        if getattr(inf, "_pg", None) is None:
            return False
        import psycopg2
        from contextlib import closing

        with closing(
            psycopg2.connect(cfg.pg_dsn(), connect_timeout=2, options="-c statement_timeout=2000")
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                return cursor.fetchone()[0] == 1

    def elasticsearch():
        client = getattr(inf, "_es", None)
        return client is not None and bool(client.options(request_timeout=2).ping())

    def milvus():
        client = getattr(inf, "_milvus", None)
        if client is None:
            return False
        client.list_collections(timeout=2)
        return True

    def kafka():
        producer = getattr(inf, "_kafka_producer", None)
        if producer is None:
            return False
        # A fresh short-lived client avoids accepting stale cached metadata.
        from kafka import KafkaProducer

        probe = KafkaProducer(
            bootstrap_servers=cfg.kafka_brokers,
            max_block_ms=2000,
            request_timeout_ms=2000,
            api_version_auto_timeout_ms=2000,
        )
        try:
            return bool(probe.partitions_for(cfg.kafka_topic))
        finally:
            probe.close(timeout=2)

    def neo4j():
        client = getattr(inf, "_neo4j_memory", None)
        if client is None or client.driver is None:
            return False
        from neo4j import Query

        with client.driver.session(default_access_mode="READ") as session:
            return session.run(Query("RETURN 1 AS ok", timeout=2)).single()["ok"] == 1

    def docker():
        sandbox = getattr(agent, "sandbox", None)
        if sandbox is None or sandbox.backend() != "docker":
            return False
        return (
            subprocess.run(
                ["docker", "version", "--format", "{{.Server.Version}}"],
                capture_output=True,
                timeout=2,
                check=False,
            ).returncode
            == 0
        )

    available = {
        "application": application,
        "run_store": runs,
        "postgresql": postgres,
        "elasticsearch": elasticsearch,
        "milvus": milvus,
        "kafka": kafka,
        "neo4j": neo4j,
        "docker": docker,
    }
    probe = ReadinessProbe({name: available[name] for name in sorted(required)})
    app.state.readiness_probe = probe
    register_shutdown(app, probe.close)
    return probe
