from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import MarkdownCleaningWorkerSettings, Settings


@pytest.fixture(scope="session", autouse=True)
def db() -> None:
    """纯配置单测不依赖 PostgreSQL。"""


def test_markdown_cleaning_worker_normalizes_staging_and_has_no_output_roots(
    tmp_path: Path,
) -> None:
    staging_root = tmp_path / "staging"
    configured = MarkdownCleaningWorkerSettings(
        staging_root=staging_root,
        max_input_bytes=1024,
    )

    assert configured.staging_root == staging_root.resolve()
    assert "output_roots" not in configured.__class__.model_fields
    with pytest.raises(ValidationError):
        MarkdownCleaningWorkerSettings(staging_root=staging_root, max_input_bytes=0)


def test_markdown_cleaning_worker_timeout_relation() -> None:
    with pytest.raises(ValidationError, match="硬超时"):
        MarkdownCleaningWorkerSettings(
            processing_soft_timeout_seconds=10,
            processing_hard_timeout_seconds=10,
        )


def test_markdown_cleaning_worker_http_cidr_must_be_valid() -> None:
    with pytest.raises(ValidationError, match="CIDR"):
        MarkdownCleaningWorkerSettings(allowed_http_cidrs=("not-a-cidr",))


def test_settings_do_not_expose_local_roots() -> None:
    configured = Settings(
        PROJECT_NAME="Text Processor",
        POSTGRES_SERVER="localhost",
        POSTGRES_USER="postgres",
        POSTGRES_PASSWORD="password",
        POSTGRES_DB="postgres",
        FIRST_SUPERUSER="admin@example.com",
        FIRST_SUPERUSER_PASSWORD="StrongPass123!",
    )
    removed = {
        "EXTRACTION_INPUT_ROOTS",
        "EXTRACTION_OUTPUT_ROOTS",
        "GLOBAL_DEDUP_INPUT_ROOTS",
        "MARKDOWN_CLEANING_INPUT_ROOTS",
        "MARKDOWN_CLEANING_OUTPUT_ROOTS",
        "CLASSIFICATION_INPUT_ROOTS",
    }

    assert removed.isdisjoint(configured.__class__.model_fields)
