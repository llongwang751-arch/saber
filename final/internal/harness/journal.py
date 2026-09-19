"""Durable compare-and-set action ledger over the application's SQL store.

A running/uncertain write is never replayed automatically after a crash.
This provides at-most-once dispatch, not exactly-once external side effects.
"""
import hashlib
import json
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from internal.application.models import AgentActionRecord, utcnow


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


class ActionJournal:
    def __init__(self, store):
        self.store = store

    def get(self, user_id, action_id):
        with self.store.transaction() as session:
            row = session.get(AgentActionRecord, (user_id, action_id))
            return None if row is None else {"status": row.status, "fingerprint": row.fingerprint, "payload": dict(row.payload)}

    def create(self, user_id, action_id, digest, status, payload):
        try:
            with self.store.transaction() as session:
                session.add(AgentActionRecord(user_id=user_id, action_id=action_id,
                    fingerprint=digest, status=status, payload=payload))
            return True
        except IntegrityError:
            return False

    def transition(self, user_id, action_id, expected, status, payload):
        with self.store.transaction() as session:
            result = session.execute(update(AgentActionRecord).where(
                AgentActionRecord.user_id == user_id, AgentActionRecord.action_id == action_id,
                AgentActionRecord.status == expected).values(status=status, payload=payload, updated_at=utcnow()))
            return result.rowcount == 1

    def list_approvals(self, user_id):
        with self.store.transaction() as session:
            rows = session.scalars(select(AgentActionRecord).where(
                AgentActionRecord.user_id == user_id,
                AgentActionRecord.action_id.like("approval:%"),
                AgentActionRecord.status.in_(["pending", "approved"]))).all()
            return [dict(row.payload) for row in rows]

    def claim_expired(self, user_id, action_id, expected, owner, payload, now):
        """Recheck expiry in the CAS itself; a heartbeat may race the prior read."""
        with self.store.transaction() as session:
            result = session.execute(update(AgentActionRecord).where(
                AgentActionRecord.user_id == user_id, AgentActionRecord.action_id == action_id,
                AgentActionRecord.status == expected,
                AgentActionRecord.payload['expires_at'].as_float() <= now
            ).values(status=owner, payload=payload, updated_at=utcnow()))
            return result.rowcount == 1
