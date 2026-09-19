"""Bind experiment lifecycle to a hash chain and durable safety outbox.

Revision ID: 0015_experiment_lifecycle_integrity
Revises: 0014_experiment_audience_identity
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0015_experiment_lifecycle_integrity"
down_revision: Union[str, Sequence[str], None] = (
    "0014_experiment_audience_identity"
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Columns are nullable during the one-time legacy checkpoint rebuild.
    # New application writes always populate all three fields.
    with op.batch_alter_table("experiment_audit_events") as batch:
        batch.add_column(sa.Column("chain_position", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column("previous_event_hash", sa.String(length=64), nullable=True)
        )
        batch.add_column(sa.Column("event_hash", sa.String(length=64), nullable=True))

    _checkpoint_existing_audit_events()

    with op.batch_alter_table("experiment_audit_events") as batch:
        batch.alter_column(
            "chain_position", existing_type=sa.Integer(), nullable=False
        )
        batch.alter_column(
            "previous_event_hash",
            existing_type=sa.String(length=64),
            nullable=False,
        )
        batch.alter_column(
            "event_hash", existing_type=sa.String(length=64), nullable=False
        )
        batch.create_unique_constraint(
            "uq_experiment_audit_chain_position",
            ["tenant_id", "experiment_id", "chain_position"],
        )

    op.create_table(
        "experiment_safety_outbox",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("experiment_id", sa.String(length=36), nullable=False),
        sa.Column("exposure_id", sa.String(length=36), nullable=False),
        sa.Column("signal_checksum", sa.String(length=64), nullable=False),
        sa.Column("failure_code", sa.String(length=100), nullable=False),
        sa.Column("safety_events", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending','processed')",
            name="ck_experiment_safety_outbox_status",
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["online_experiments.id"],
            name="fk_experiment_safety_outbox_experiment",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["exposure_id"],
            ["experiment_exposures.id"],
            name="fk_experiment_safety_outbox_exposure",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_experiment_safety_outbox"),
        sa.UniqueConstraint(
            "tenant_id",
            "exposure_id",
            "signal_checksum",
            name="uq_experiment_safety_outbox_signal",
        ),
    )
    op.create_index(
        "ix_experiment_safety_outbox_pending",
        "experiment_safety_outbox",
        ["tenant_id", "status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_experiment_safety_outbox_pending",
        table_name="experiment_safety_outbox",
    )
    op.drop_table("experiment_safety_outbox")
    with op.batch_alter_table("experiment_audit_events") as batch:
        batch.drop_constraint(
            "uq_experiment_audit_chain_position", type_="unique"
        )
        batch.drop_column("event_hash")
        batch.drop_column("previous_event_hash")
        batch.drop_column("chain_position")


def _checkpoint_existing_audit_events() -> None:
    """Turn pre-0015 history into a deterministic local integrity chain."""

    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT id, tenant_id, experiment_id, deployment_id, action, actor, "
            "from_status, to_status, generation, request_hash, idempotency_key, "
            "details, result, created_at FROM experiment_audit_events"
        )
    ).mappings().all()

    experiment_groups: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    deployment_only: list[Mapping[str, Any]] = []
    for row in rows:
        if row["experiment_id"] is None:
            deployment_only.append(row)
        else:
            experiment_groups.setdefault(
                (str(row["tenant_id"]), str(row["experiment_id"])), []
            ).append(row)

    for group in experiment_groups.values():
        group.sort(
            key=lambda item: (
                int(item["generation"] or 0),
                _iso_utc(item["created_at"]),
                str(item["id"]),
            )
        )
        previous_hash = ""
        previous_status = ""
        for position, row in enumerate(group, start=1):
            result = _json_value(row["result"])
            state = result.get("experiment") if isinstance(result, dict) else None
            if not isinstance(state, dict):
                state = result if isinstance(result, dict) else {}
            to_status = str(state.get("status") or row["to_status"] or "")
            generation = int(state.get("generation", row["generation"] or 0))
            values = _event_values(
                row,
                from_status=previous_status,
                to_status=to_status,
                generation=generation,
                chain_position=position,
                previous_event_hash=previous_hash,
            )
            event_hash = _canonical_sha256(values)
            connection.execute(
                sa.text(
                    "UPDATE experiment_audit_events SET from_status=:from_status, "
                    "to_status=:to_status, generation=:generation, "
                    "chain_position=:chain_position, "
                    "previous_event_hash=:previous_event_hash, event_hash=:event_hash "
                    "WHERE id=:id"
                ),
                {**values, "event_hash": event_hash},
            )
            previous_hash = event_hash
            previous_status = to_status

    for row in deployment_only:
        values = _event_values(
            row,
            from_status=str(row["from_status"] or ""),
            to_status=str(row["to_status"] or ""),
            generation=int(row["generation"] or 0),
            chain_position=0,
            previous_event_hash="",
        )
        connection.execute(
            sa.text(
                "UPDATE experiment_audit_events SET chain_position=0, "
                "previous_event_hash='', event_hash=:event_hash WHERE id=:id"
            ),
            {"id": row["id"], "event_hash": _canonical_sha256(values)},
        )


def _event_values(
    row: Mapping[str, Any],
    *,
    from_status: str,
    to_status: str,
    generation: int,
    chain_position: int,
    previous_event_hash: str,
) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "tenant_id": str(row["tenant_id"]),
        "experiment_id": row["experiment_id"],
        "deployment_id": row["deployment_id"],
        "action": str(row["action"]),
        "actor": str(row["actor"]),
        "from_status": from_status,
        "to_status": to_status,
        "generation": generation,
        "request_hash": str(row["request_hash"]),
        "idempotency_key": str(row["idempotency_key"]),
        "details": _json_value(row["details"]),
        "result": _json_value(row["result"]),
        "chain_position": chain_position,
        "previous_event_hash": previous_event_hash,
        "created_at": _iso_utc(row["created_at"]),
    }


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _iso_utc(value: Any) -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()
