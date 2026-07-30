"""add extraction tasks

Revision ID: 20260730_01
Revises: fe56fa70289e
Create Date: 2026-07-30 15:20:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260730_01"
down_revision: str | None = "fe56fa70289e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "extraction_task",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("caller_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("file_id", sa.String(length=128), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("file_storage_path", sa.String(length=2048), nullable=True),
        sa.Column("file_oss_url", sa.String(length=4096), nullable=True),
        sa.Column("selected_input_type", sa.String(length=16), nullable=False),
        sa.Column("target_path", sa.String(length=2048), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("processing_phase", sa.String(length=64), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "prepared_output_sha256",
            sa.String(length=64),
            nullable=True,
        ),
        sa.Column("staging_path", sa.String(length=2048), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_metadata", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["caller_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "caller_id",
            "session_id",
            "file_id",
            name="uq_extraction_task_caller_session_file",
        ),
    )
    op.create_index(
        op.f("ix_extraction_task_caller_id"),
        "extraction_task",
        ["caller_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_extraction_task_status"),
        "extraction_task",
        ["status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_extraction_task_status"), table_name="extraction_task")
    op.drop_index(op.f("ix_extraction_task_caller_id"), table_name="extraction_task")
    op.drop_table("extraction_task")
