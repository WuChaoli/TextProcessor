from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.config import MarkdownCleaningWorkerSettings, Settings


def test_markdown_cleaning_worker_root_validation(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="清洗"):
        MarkdownCleaningWorkerSettings(staging_root=tmp_path, output_roots=(tmp_path,))


def test_markdown_cleaning_worker_normalize_roots_and_require_positive_limits(
    tmp_path: Path,
) -> None:
    staging_root = tmp_path / "staging"
    output_root = tmp_path / "output"

    configured = MarkdownCleaningWorkerSettings(
        staging_root=staging_root,
        output_roots=(output_root,),
        max_input_bytes=1024,
    )

    assert configured.staging_root == staging_root.resolve()
    assert configured.output_roots == (output_root.resolve(),)
    with pytest.raises(ValidationError):
        MarkdownCleaningWorkerSettings(
            staging_root=staging_root,
            output_roots=(output_root,),
            max_input_bytes=0,
        )


def test_markdown_cleaning_worker_timeout_relation() -> None:
    with pytest.raises(ValidationError, match="硬超时"):
        MarkdownCleaningWorkerSettings(
            processing_soft_timeout_seconds=10,
            processing_hard_timeout_seconds=10,
        )


def test_markdown_cleaning_worker_http_cidr_must_be_valid() -> None:
    with pytest.raises(ValidationError, match="CIDR"):
        MarkdownCleaningWorkerSettings(allowed_http_cidrs=("not-a-cidr",))


def _markdown_cleaning_settings_kwargs(
    *,
    worker_staging_root: Path,
    worker_output_roots: tuple[Path, ...],
) -> dict[str, Any]:
    return {
        "PROJECT_NAME": "Text Processor",
        "POSTGRES_SERVER": "localhost",
        "POSTGRES_USER": "postgres",
        "POSTGRES_PASSWORD": "password",
        "POSTGRES_DB": "postgres",
        "FIRST_SUPERUSER": "admin@example.com",
        "FIRST_SUPERUSER_PASSWORD": "StrongPass123!",
        "MARKDOWN_CLEANING_WORKER": MarkdownCleaningWorkerSettings(
            staging_root=worker_staging_root,
            output_roots=worker_output_roots,
        ),
    }


def test_markdown_cleaning_api_roots_cannot_overlap_worker_staging_root(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValidationError, match="worker"):
        Settings(
            **_markdown_cleaning_settings_kwargs(
                worker_staging_root=tmp_path / "worker_root",
                worker_output_roots=(tmp_path / "worker_output",),
            ),
            MARKDOWN_CLEANING_INPUT_ROOTS=[tmp_path / "worker_root" / "input"],
        )


def test_markdown_cleaning_api_roots_cannot_overlap_worker_output_root(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValidationError, match="输出任务"):
        Settings(
            **_markdown_cleaning_settings_kwargs(
                worker_staging_root=tmp_path / "worker_root",
                worker_output_roots=(tmp_path / "worker_output",),
            ),
            MARKDOWN_CLEANING_OUTPUT_ROOTS=[tmp_path / "worker_output" / "child"],
        )


def test_markdown_cleaning_api_output_root_overlap_with_worker_output_is_invalid(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValidationError, match="输出任务"):
        Settings(
            **_markdown_cleaning_settings_kwargs(
                worker_staging_root=tmp_path / "worker_root",
                worker_output_roots=(tmp_path / "worker_output",),
            ),
            MARKDOWN_CLEANING_OUTPUT_ROOTS=[tmp_path / "worker_output"],
        )


def test_markdown_cleaning_api_input_root_overlap_with_worker_root_is_invalid(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValidationError, match="worker"):
        Settings(
            **_markdown_cleaning_settings_kwargs(
                worker_staging_root=tmp_path / "worker_root",
                worker_output_roots=(tmp_path / "worker_output",),
            ),
            MARKDOWN_CLEANING_INPUT_ROOTS=[tmp_path / "worker_root"],
        )
