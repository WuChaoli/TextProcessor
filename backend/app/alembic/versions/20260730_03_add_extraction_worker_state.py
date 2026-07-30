"""add extraction worker state

Revision ID: 20260730_03
Revises: 20260730_02
Create Date: 2026-07-30 17:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260730_03"
down_revision: str | None = "20260730_02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "extraction_task",
        sa.Column("detected_format", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "extraction_task",
        sa.Column("routing_reasons", sa.JSON(), nullable=True),
    )
    op.add_column(
        "extraction_task",
        sa.Column("processor_name", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "extraction_task",
        sa.Column("processor_version", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "extraction_task",
        sa.Column("profile_name", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "extraction_task",
        sa.Column("profile_sha256", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "extraction_task",
        sa.Column("external_task_id", sa.String(length=256), nullable=True),
    )
    op.add_column(
        "extraction_task",
        sa.Column("next_poll_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "extraction_task",
        sa.Column("processing_deadline", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "extraction_task",
        sa.Column("poll_lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "extraction_task",
        sa.Column("input_sha256", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "extraction_task",
        sa.Column("input_size_bytes", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "extraction_task",
        sa.Column("output_sha256", sa.String(length=64), nullable=True),
    )
    op.create_index(
        op.f("ix_extraction_task_next_poll_at"),
        "extraction_task",
        ["next_poll_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_extraction_task_next_poll_at"),
        table_name="extraction_task",
    )
    op.drop_column("extraction_task", "output_sha256")
    op.drop_column("extraction_task", "input_size_bytes")
    op.drop_column("extraction_task", "input_sha256")
    op.drop_column("extraction_task", "poll_lease_expires_at")
    op.drop_column("extraction_task", "processing_deadline")
    op.drop_column("extraction_task", "next_poll_at")
    op.drop_column("extraction_task", "external_task_id")
    op.drop_column("extraction_task", "profile_sha256")
    op.drop_column("extraction_task", "profile_name")
    op.drop_column("extraction_task", "processor_version")
    op.drop_column("extraction_task", "processor_name")
    op.drop_column("extraction_task", "routing_reasons")
    op.drop_column("extraction_task", "detected_format")
