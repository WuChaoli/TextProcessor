"""set markdown cleaning task defaults for existing tables

Revision ID: 20260803_02
Revises: 20260803_01
Create Date: 2026-08-03 15:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260803_02"
down_revision: str | None = "20260803_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "markdown_cleaning_task",
        "processor_contract_version",
        server_default="markdown_cleaning_v1",
        existing_type=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "markdown_cleaning_task",
        "max_attempts",
        server_default="3",
        existing_type=sa.Integer(),
        existing_nullable=False,
    )


def downgrade() -> None:
    # No-op for downgrade: baseline migration 20260803_01 already creates defaults.
    return

