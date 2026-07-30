"""add extraction dispatch state

Revision ID: 20260730_02
Revises: 20260730_01
Create Date: 2026-07-30 16:05:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260730_02"
down_revision: str | None = "20260730_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "extraction_task",
        sa.Column(
            "last_dispatched_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_index(
        op.f("ix_extraction_task_last_dispatched_at"),
        "extraction_task",
        ["last_dispatched_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_extraction_task_last_dispatched_at"),
        table_name="extraction_task",
    )
    op.drop_column("extraction_task", "last_dispatched_at")
