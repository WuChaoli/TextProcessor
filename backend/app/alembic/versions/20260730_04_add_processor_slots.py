"""add processor slots

Revision ID: 20260730_04
Revises: 20260730_03
Create Date: 2026-07-30 18:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260730_04"
down_revision: str | None = "20260730_03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "processor_slot",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("processor_name", sa.String(length=32), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("quarantined_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["extraction_task.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id"),
    )
    op.create_index(
        op.f("ix_processor_slot_processor_name"),
        "processor_slot",
        ["processor_name"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_processor_slot_processor_name"),
        table_name="processor_slot",
    )
    op.drop_table("processor_slot")
