"""add classification tasks

Revision ID: 20260805_01
Revises: 20260803_03
Create Date: 2026-08-05 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260805_01"
down_revision: str | None = "20260803_03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "classification_task",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("caller_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", sa.String(128), nullable=False),
        sa.Column("file_id", sa.String(128), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("input_uri", sa.String(4096), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("staging_uri", sa.String(4096), nullable=True),
        sa.Column("input_sha256", sa.String(64), nullable=True),
        sa.Column("input_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.String(512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["caller_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("caller_id", "session_id", "file_id", name="uq_classification_task_caller_session_file"),
    )
    op.create_index(op.f("ix_classification_task_caller_id"), "classification_task", ["caller_id"])
    op.create_index(op.f("ix_classification_task_status"), "classification_task", ["status"])


def downgrade() -> None:
    op.drop_index(op.f("ix_classification_task_status"), table_name="classification_task")
    op.drop_index(op.f("ix_classification_task_caller_id"), table_name="classification_task")
    op.drop_table("classification_task")
