"""add classification recovery state

Revision ID: 20260805_02
Revises: 20260805_01
Create Date: 2026-08-05 13:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260805_02"
down_revision: str | None = "20260805_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("classification_task", sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False))
    op.add_column("classification_task", sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False))
    op.add_column("classification_task", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint("ck_classification_task_attempts", "classification_task", "attempt_count >= 0 AND max_attempts > 0")
    op.create_index(op.f("ix_classification_task_lease_expires_at"), "classification_task", ["lease_expires_at"])


def downgrade() -> None:
    op.drop_index(op.f("ix_classification_task_lease_expires_at"), table_name="classification_task")
    op.drop_constraint("ck_classification_task_attempts", "classification_task", type_="check")
    op.drop_column("classification_task", "lease_expires_at")
    op.drop_column("classification_task", "max_attempts")
    op.drop_column("classification_task", "attempt_count")
