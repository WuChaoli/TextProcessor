"""add markdown cleaning tasks

Revision ID: 20260803_01
Revises: 20260731_01
Create Date: 2026-08-03 08:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260803_01"
down_revision: str | None = "20260731_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "markdown_cleaning_task",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("caller_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("file_id", sa.String(length=255), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("file_storage_path", sa.String(length=4096), nullable=True),
        sa.Column("file_oss_url", sa.String(length=4096), nullable=True),
        sa.Column("selected_input_type", sa.String(length=16), nullable=False),
        sa.Column("target_path", sa.String(length=4096), nullable=False),
        sa.Column(
            "processor_contract_version",
            sa.String(length=64),
            nullable=False,
            server_default="markdown_cleaning_v1",
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("processing_phase", sa.String(length=64), nullable=True),
        sa.Column("progress_percent", sa.Integer(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column(
            "max_attempts",
            sa.Integer(),
            nullable=False,
            server_default="3",
        ),
        sa.Column("lease_token", sa.String(length=128), nullable=True),
        sa.Column(
            "lease_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("staging_path", sa.String(length=4096), nullable=True),
        sa.Column("input_sha256", sa.String(length=64), nullable=True),
        sa.Column("prepared_output_sha256", sa.String(length=64), nullable=True),
        sa.Column("output_sha256", sa.String(length=64), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duplicate_paragraphs_removed", sa.Integer(), nullable=True),
        sa.Column("phone_redaction_count", sa.Integer(), nullable=True),
        sa.Column("id_card_redaction_count", sa.Integer(), nullable=True),
        sa.Column("bank_card_redaction_count", sa.Integer(), nullable=True),
        sa.Column("email_redaction_count", sa.Integer(), nullable=True),
        sa.Column("ipv4_redaction_count", sa.Integer(), nullable=True),
        sa.Column("formatting_change_count", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("attempt_count >= 0 AND max_attempts > 0", name="ck_markdown_cleaning_attempts"),
        sa.CheckConstraint(
            "progress_percent >= 0 AND progress_percent <= 100",
            name="ck_markdown_cleaning_progress_percent",
        ),
        sa.CheckConstraint(
            "duplicate_paragraphs_removed IS NULL OR duplicate_paragraphs_removed >= 0",
            name="ck_markdown_cleaning_duplicate_paragraphs_removed",
        ),
        sa.CheckConstraint(
            "phone_redaction_count IS NULL OR phone_redaction_count >= 0",
            name="ck_markdown_cleaning_phone_redaction_count",
        ),
        sa.CheckConstraint(
            "id_card_redaction_count IS NULL OR id_card_redaction_count >= 0",
            name="ck_markdown_cleaning_id_card_redaction_count",
        ),
        sa.CheckConstraint(
            "bank_card_redaction_count IS NULL OR bank_card_redaction_count >= 0",
            name="ck_markdown_cleaning_bank_card_redaction_count",
        ),
        sa.CheckConstraint(
            "email_redaction_count IS NULL OR email_redaction_count >= 0",
            name="ck_markdown_cleaning_email_redaction_count",
        ),
        sa.CheckConstraint(
            "ipv4_redaction_count IS NULL OR ipv4_redaction_count >= 0",
            name="ck_markdown_cleaning_ipv4_redaction_count",
        ),
        sa.CheckConstraint(
            "formatting_change_count IS NULL OR formatting_change_count >= 0",
            name="ck_markdown_cleaning_formatting_change_count",
        ),
        sa.ForeignKeyConstraint(["caller_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "caller_id",
            "session_id",
            "file_id",
            name="uq_markdown_cleaning_caller_session_file",
        ),
    )
    for column in (
        "caller_id",
        "status",
        "lease_expires_at",
        "queued_at",
        "last_dispatched_at",
    ):
        op.create_index(
            op.f(f"ix_markdown_cleaning_task_{column}"),
            "markdown_cleaning_task",
            [column],
            unique=False,
        )


def downgrade() -> None:
    for column in reversed(
        (
            "caller_id",
            "status",
            "lease_expires_at",
            "queued_at",
            "last_dispatched_at",
        )
    ):
        op.drop_index(
            op.f(f"ix_markdown_cleaning_task_{column}"),
            table_name="markdown_cleaning_task",
        )
    op.drop_table("markdown_cleaning_task")
