import os
import stat
import uuid
from pathlib import Path

import pytest

from app.features.markdown_cleaning.staging import StagingLayout

TASK_ID = uuid.UUID("018f0000-0000-7000-8000-000000000002")


def test_layout_is_derived_only_from_staging_root_and_task_id(tmp_path: Path) -> None:
    layout = StagingLayout.for_task(tmp_path / "staging", TASK_ID)

    assert layout.root == (tmp_path / "staging").resolve() / str(TASK_ID)
    assert layout.input_dir == layout.root / "input"
    assert layout.original_source == layout.input_dir / "source.original.md"
    assert layout.processor_source == layout.input_dir / "source.md"
    assert layout.output_dir == layout.root / "output"
    assert layout.result == layout.output_dir / "result.md"
    assert layout.publish_part == layout.output_dir / "publish.md.part"


def test_prepare_creates_private_task_directories(tmp_path: Path) -> None:
    layout = StagingLayout.for_task(tmp_path / "staging", TASK_ID)

    layout.prepare()

    assert layout.input_dir.is_dir()
    assert layout.output_dir.is_dir()
    if os.name != "nt":
        assert stat.S_IMODE(layout.root.stat().st_mode) == 0o700
        assert stat.S_IMODE(layout.input_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(layout.output_dir.stat().st_mode) == 0o700


def test_prepare_rejects_symlinked_task_directory(tmp_path: Path) -> None:
    staging_root = tmp_path / "staging"
    staging_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    task_root = staging_root / str(TASK_ID)
    try:
        task_root.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("当前环境不允许创建目录符号链接")

    with pytest.raises(ValueError, match="staging"):
        StagingLayout.for_task(staging_root, TASK_ID).prepare()

    assert not (outside / "input").exists()


def test_prepare_rejects_symlinked_input_directory(tmp_path: Path) -> None:
    staging_root = tmp_path / "staging"
    task_root = staging_root / str(TASK_ID)
    task_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "marker.txt"
    marker.write_text("keep", encoding="utf-8")
    try:
        (task_root / "input").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("当前环境不允许创建目录符号链接")

    with pytest.raises(ValueError, match="staging"):
        StagingLayout.for_task(staging_root, TASK_ID).prepare()

    assert marker.read_text(encoding="utf-8") == "keep"


def test_cleanup_recomputes_task_root_and_removes_only_that_task(
    tmp_path: Path,
) -> None:
    staging_root = tmp_path / "staging"
    layout = StagingLayout.for_task(staging_root, TASK_ID)
    other = StagingLayout.for_task(staging_root, uuid.uuid4())
    layout.prepare()
    other.prepare()
    layout.original_source.write_bytes(b"owned")
    other.original_source.write_bytes(b"other")

    layout.cleanup()

    assert not layout.root.exists()
    assert other.original_source.read_bytes() == b"other"


def test_cleanup_unlinks_child_reparse_without_traversing_external_target(
    tmp_path: Path,
) -> None:
    layout = StagingLayout.for_task(tmp_path / "staging", TASK_ID)
    layout.prepare()
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "marker.txt"
    marker.write_text("keep", encoding="utf-8")
    try:
        (layout.input_dir / "linked").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("当前环境不允许创建目录符号链接")

    layout.cleanup()

    assert marker.read_text(encoding="utf-8") == "keep"
    assert not layout.root.exists()


def test_cleanup_rejects_symlinked_task_root_without_deleting_target(
    tmp_path: Path,
) -> None:
    staging_root = tmp_path / "staging"
    staging_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "marker.txt"
    marker.write_text("keep", encoding="utf-8")
    try:
        (staging_root / str(TASK_ID)).symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("当前环境不允许创建目录符号链接")

    with pytest.raises(ValueError, match="staging"):
        StagingLayout.for_task(staging_root, TASK_ID).cleanup()

    assert marker.read_text(encoding="utf-8") == "keep"


def test_cleanup_rejects_task_root_replaced_at_quarantine_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging_root = tmp_path / "staging"
    layout = StagingLayout.for_task(staging_root, TASK_ID)
    layout.prepare()
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "marker.txt"
    marker.write_text("keep", encoding="utf-8")
    original_replace = os.replace

    def replace_with_link(source: Path, destination: Path) -> None:
        if Path(source) == layout.root:
            layout.root.rename(staging_root / "displaced")
            try:
                layout.root.symlink_to(outside, target_is_directory=True)
            except OSError:
                pytest.skip("当前环境不允许创建目录符号链接")
        original_replace(source, destination)

    monkeypatch.setattr(os, "replace", replace_with_link)

    with pytest.raises(ValueError, match="staging"):
        layout.cleanup()

    assert marker.read_text(encoding="utf-8") == "keep"


def test_task_id_must_be_uuid(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="UUID"):
        StagingLayout.for_task(
            tmp_path / "staging",
            "../outside",  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
        )
