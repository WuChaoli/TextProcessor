"""add markdown cleaning processing deadline

Revision ID: 20260803_03
Revises: 20260803_02
Create Date: 2026-08-04 02:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260803_03"
down_revision: str | None = "20260803_02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "markdown_cleaning_task",
        sa.Column("processing_deadline", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        op.f("ix_markdown_cleaning_task_processing_deadline"),
        "markdown_cleaning_task",
        ["processing_deadline"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_markdown_cleaning_task_processing_deadline"),
        table_name="markdown_cleaning_task",
    )
    op.drop_column("markdown_cleaning_task", "processing_deadline")
