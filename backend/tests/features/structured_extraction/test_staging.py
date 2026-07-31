import uuid
from pathlib import Path

import pytest

from app.features.structured_extraction.staging import StagingLayout

TASK_ID = uuid.UUID("018f0000-0000-7000-8000-000000000001")


def test_layout_is_derived_only_from_task_id(tmp_path: Path) -> None:
    layout = StagingLayout.for_task(tmp_path, TASK_ID)

    assert layout.root == tmp_path / str(TASK_ID)
    assert layout.source.parent == layout.root / "source"
    assert layout.processor_dir == layout.root / "processor"
    assert layout.output == layout.root / "output" / "result.md"


def test_layout_creates_private_directories(tmp_path: Path) -> None:
    layout = StagingLayout.for_task(tmp_path, TASK_ID)

    layout.prepare()

    assert layout.source.parent.is_dir()
    assert layout.processor_dir.is_dir()
    assert layout.output.parent.is_dir()


def test_cleanup_rejects_layout_outside_staging_root(tmp_path: Path) -> None:
    layout = StagingLayout(
        staging_root=tmp_path / "staging",
        task_id=TASK_ID,
        root=tmp_path / "other",
        source=tmp_path / "other" / "source" / "original",
        processor_dir=tmp_path / "other" / "processor",
        output=tmp_path / "other" / "output" / "result.md",
    )

    with pytest.raises(ValueError, match="staging"):
        layout.cleanup()
