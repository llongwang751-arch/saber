"""Add application identity and installed skill tables.

Revision ID: 0005_application_identity_skills
Revises: 0004_evaluation_indexes
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_application_identity_skills"
down_revision: Union[str, Sequence[str], None] = "0004_evaluation_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("username", sa.String(32), nullable=False),
        sa.Column("password_hash", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("username", name="uq_users_username"),
    )
    op.create_table(
        "installed_skills",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("skill_id", sa.String(300), nullable=False),
        sa.Column("name", sa.String(300), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("category", sa.String(100), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("stars", sa.Integer(), nullable=False),
        sa.Column("invocation", sa.String(32), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("prompt_template", sa.Text(), nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("installed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "skill_id", name="uq_installed_skills_user_skill"),
    )
    op.create_index("ix_installed_skills_user_enabled", "installed_skills", ["user_id", "enabled"])


def downgrade() -> None:
    op.drop_index("ix_installed_skills_user_enabled", table_name="installed_skills")
    op.drop_table("installed_skills")
    op.drop_table("users")

