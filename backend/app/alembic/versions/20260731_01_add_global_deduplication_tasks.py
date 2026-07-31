"""add global deduplication tasks

Revision ID: 20260731_01
Revises: 20260730_04
Create Date: 2026-07-31 17:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260731_01"
down_revision: str | None = "20260730_04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "global_deduplication_task",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("caller_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("input_json_path", sa.String(length=4096), nullable=False),
        sa.Column("target_path", sa.String(length=4096), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("processing_phase", sa.String(length=64), nullable=True),
        sa.Column("progress_total", sa.Integer(), nullable=True),
        sa.Column("progress_processed", sa.Integer(), nullable=False),
        sa.Column("progress_percent", sa.Integer(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "external_job_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("external_profile", sa.String(length=128), nullable=True),
        sa.Column("external_status", sa.String(length=32), nullable=True),
        sa.Column("external_progress", sa.JSON(), nullable=True),
        sa.Column("next_poll_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processing_deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "poll_lease_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "external_recovery_attempted",
            sa.Boolean(),
            nullable=False,
        ),
        sa.Column("staging_path", sa.String(length=4096), nullable=True),
        sa.Column("input_manifest_sha256", sa.String(length=64), nullable=True),
        sa.Column("input_jsonl_sha256", sa.String(length=64), nullable=True),
        sa.Column("mapping_sha256", sa.String(length=64), nullable=True),
        sa.Column("external_output_sha256", sa.String(length=64), nullable=True),
        sa.Column("prepared_output_sha256", sa.String(length=64), nullable=True),
        sa.Column("output_sha256", sa.String(length=64), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_metadata", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "progress_total IS NULL OR progress_total >= 0",
            name="ck_global_dedup_progress_total",
        ),
        sa.CheckConstraint(
            "progress_processed >= 0",
            name="ck_global_dedup_progress_processed",
        ),
        sa.CheckConstraint(
            "progress_percent >= 0 AND progress_percent <= 100",
            name="ck_global_dedup_progress_percent",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0 AND max_attempts > 0",
            name="ck_global_dedup_attempts",
        ),
        sa.ForeignKeyConstraint(["caller_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "caller_id",
            "session_id",
            name="uq_global_deduplication_caller_session",
        ),
    )
    for column in (
        "caller_id",
        "status",
        "lease_expires_at",
        "external_job_id",
        "next_poll_at",
        "poll_lease_expires_at",
        "queued_at",
    ):
        op.create_index(
            op.f(f"ix_global_deduplication_task_{column}"),
            "global_deduplication_task",
            [column],
            unique=False,
        )


def downgrade() -> None:
    for column in reversed(
        (
            "caller_id",
            "status",
            "lease_expires_at",
            "external_job_id",
            "next_poll_at",
            "poll_lease_expires_at",
            "queued_at",
        )
    ):
        op.drop_index(
            op.f(f"ix_global_deduplication_task_{column}"),
            table_name="global_deduplication_task",
        )
    op.drop_table("global_deduplication_task")
