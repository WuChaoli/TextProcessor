"""创建 Data-Juicer job 表。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260731_01"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "datajuicer_jobs",
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.String(length=255), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("profile", sa.String(length=64), nullable=False),
        sa.Column("input_path", sa.Text(), nullable=False),
        sa.Column("output_path", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "queued",
                "running",
                "succeeded",
                "failed",
                "cancelled",
                name="jobstatus",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("processing_phase", sa.String(length=64), nullable=False),
        sa.Column("progress_total", sa.Integer(), nullable=True),
        sa.Column("progress_processed", sa.Integer(), nullable=False),
        sa.Column("progress_percent", sa.Integer(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processing_deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("input_sha256", sa.String(length=64), nullable=True),
        sa.Column("input_count", sa.Integer(), nullable=True),
        sa.Column("prepared_output_sha256", sa.String(length=64), nullable=True),
        sa.Column("staging_output_path", sa.Text(), nullable=True),
        sa.Column("output_sha256", sa.String(length=64), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "NOT (output_sha256 IS NOT NULL AND error_code IS NOT NULL)",
            name="ck_datajuicer_job_result_error_exclusive",
        ),
        sa.PrimaryKeyConstraint("job_id"),
    )
    op.create_index(
        "ix_datajuicer_jobs_lease_expires",
        "datajuicer_jobs",
        ["lease_expires_at"],
    )
    op.create_index(
        "ix_datajuicer_jobs_request_id",
        "datajuicer_jobs",
        ["request_id"],
        unique=True,
    )
    op.create_index(
        "ix_datajuicer_jobs_status_updated",
        "datajuicer_jobs",
        ["status", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_datajuicer_jobs_status_updated", table_name="datajuicer_jobs")
    op.drop_index("ix_datajuicer_jobs_request_id", table_name="datajuicer_jobs")
    op.drop_index("ix_datajuicer_jobs_lease_expires", table_name="datajuicer_jobs")
    op.drop_table("datajuicer_jobs")
