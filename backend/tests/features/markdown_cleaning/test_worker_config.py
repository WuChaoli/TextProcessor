from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.config import MarkdownCleaningWorkerSettings, Settings


@pytest.fixture(scope="session", autouse=True)
def db() -> None:
    """Override the backend-wide database fixture for pure config tests."""


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


def test_markdown_cleaning_api_output_may_equal_worker_output_root(
    tmp_path: Path,
) -> None:
    output = tmp_path / "worker_output"
    configured = Settings(
        **_markdown_cleaning_settings_kwargs(
            worker_staging_root=tmp_path / "worker_root",
            worker_output_roots=(output,),
        ),
        MARKDOWN_CLEANING_OUTPUT_ROOTS=[output],
    )
    assert configured.MARKDOWN_CLEANING_OUTPUT_ROOTS == [output.resolve()]


def test_markdown_cleaning_api_output_child_may_share_worker_output_boundary(
    tmp_path: Path,
) -> None:
    worker_output = tmp_path / "worker_output"
    child = worker_output / "child"
    configured = Settings(
        **_markdown_cleaning_settings_kwargs(
            worker_staging_root=tmp_path / "worker_root",
            worker_output_roots=(worker_output,),
        ),
        MARKDOWN_CLEANING_OUTPUT_ROOTS=[child],
    )
    assert configured.MARKDOWN_CLEANING_OUTPUT_ROOTS == [child.resolve()]


@pytest.mark.parametrize("api_output", ["input", "staging", "staging/child"])
def test_markdown_cleaning_api_output_rejects_input_or_staging_overlap(
    tmp_path: Path, api_output: str
) -> None:
    input_root = tmp_path / "input"
    staging_root = tmp_path / "staging"
    output = input_root if api_output == "input" else tmp_path / api_output
    with pytest.raises(ValidationError, match="Markdown"):
        Settings(
            **_markdown_cleaning_settings_kwargs(
                worker_staging_root=staging_root,
                worker_output_roots=(tmp_path / "worker_output",),
            ),
            MARKDOWN_CLEANING_INPUT_ROOTS=[input_root],
            MARKDOWN_CLEANING_OUTPUT_ROOTS=[output],
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
