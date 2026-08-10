"""replace global deduplication manifest paths with directory input

Revision ID: 20260810_01
Revises: 20260805_02
Create Date: 2026-08-10 16:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260810_01"
down_revision: str | None = "20260805_02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "global_deduplication_task",
        sa.Column("input_path", sa.String(length=4096), nullable=True),
    )
    op.execute(
        "UPDATE global_deduplication_task SET status = 'failed', "
        "error_code = 'INTERNAL_ERROR', error_message = '历史任务不支持目录迁移', "
        "finished_at = CURRENT_TIMESTAMP "
        "WHERE status IN ('pending', 'queued', 'running')"
    )
    op.execute(
        "UPDATE global_deduplication_task SET input_path = input_json_path "
        "WHERE input_path IS NULL"
    )
    op.alter_column("global_deduplication_task", "input_path", nullable=False)
    op.drop_column("global_deduplication_task", "target_path")
    op.drop_column("global_deduplication_task", "input_json_path")


def downgrade() -> None:
    op.add_column(
        "global_deduplication_task",
        sa.Column("target_path", sa.String(length=4096), nullable=True),
    )
    op.add_column(
        "global_deduplication_task",
        sa.Column("input_json_path", sa.String(length=4096), nullable=True),
    )
    op.execute(
        "UPDATE global_deduplication_task SET input_json_path = input_path, "
        "target_path = ''"
    )
    op.drop_column("global_deduplication_task", "input_path")
