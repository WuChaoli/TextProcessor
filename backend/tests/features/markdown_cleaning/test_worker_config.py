from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import MarkdownCleaningWorkerSettings


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
