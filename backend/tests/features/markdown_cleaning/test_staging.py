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


def test_prepare_rejects_symlinked_staging_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    staging_root = tmp_path / "staging"
    try:
        staging_root.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("当前环境不允许创建目录符号链接")

    with pytest.raises(ValueError, match="staging"):
        StagingLayout.for_task(staging_root, TASK_ID).prepare()

    assert not (outside / str(TASK_ID)).exists()


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


def test_prepare_root_swap_cannot_create_task_under_external_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging_root = tmp_path / "staging"
    staging_root.mkdir()
    displaced_root = tmp_path / "staging-displaced"
    outside = tmp_path / "outside"
    outside.mkdir()
    layout = StagingLayout.for_task(staging_root, TASK_ID)
    original_mkdir = os.mkdir
    original_rename = os.rename
    attack_attempted = False
    attack_blocked = False
    root_swapped = False

    def mkdir_with_root_swap(
        path: os.PathLike[str] | str,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal attack_attempted, attack_blocked, root_swapped
        candidate = Path(path)
        if not attack_attempted and candidate.name == str(TASK_ID):
            attack_attempted = True
            try:
                original_rename(staging_root, displaced_root)
                os.symlink(outside, staging_root, target_is_directory=True)
            except OSError:
                attack_blocked = True
            else:
                root_swapped = True
        if dir_fd is None:
            original_mkdir(path, mode)
        else:
            original_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "mkdir", mkdir_with_root_swap)

    operation_rejected = False
    try:
        layout.prepare()
    except ValueError:
        operation_rejected = True

    assert attack_attempted
    assert attack_blocked or (root_swapped and operation_rejected)
    assert not (outside / str(TASK_ID)).exists()

    if attack_blocked:
        monkeypatch.undo()
        layout.staging_root.rename(displaced_root)
        assert displaced_root.is_dir()


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
    original_rename = os.rename
    original_replace = os.replace

    def replace_task_with_link(
        source: os.PathLike[str] | str,
        destination: os.PathLike[str] | str,
        operation: object,
        *,
        src_dir_fd: int | None,
        dst_dir_fd: int | None,
    ) -> None:
        if Path(source).name == str(TASK_ID):
            displaced = "displaced"
            if src_dir_fd is None:
                original_rename(layout.root, staging_root / displaced)
            else:
                original_rename(
                    str(TASK_ID),
                    displaced,
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=src_dir_fd,
                )
            try:
                os.symlink(
                    outside,
                    str(TASK_ID) if src_dir_fd is not None else layout.root,
                    target_is_directory=True,
                    dir_fd=src_dir_fd,
                )
            except OSError:
                pytest.skip("当前环境不允许创建目录符号链接")
        if operation is original_rename:
            original_rename(
                source,
                destination,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
            )
        else:
            original_replace(
                source,
                destination,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
            )

    def rename_with_link(
        source: os.PathLike[str] | str,
        destination: os.PathLike[str] | str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        replace_task_with_link(
            source,
            destination,
            original_rename,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    def replace_with_link(
        source: os.PathLike[str] | str,
        destination: os.PathLike[str] | str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        replace_task_with_link(
            source,
            destination,
            original_replace,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(os, "rename", rename_with_link)
    monkeypatch.setattr(os, "replace", replace_with_link)

    with pytest.raises(ValueError, match="staging"):
        layout.cleanup()

    assert marker.read_text(encoding="utf-8") == "keep"


def test_cleanup_root_swap_cannot_delete_external_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging_root = tmp_path / "staging"
    layout = StagingLayout.for_task(staging_root, TASK_ID)
    layout.prepare()
    layout.original_source.write_text("owned", encoding="utf-8")
    displaced_root = tmp_path / "staging-displaced"
    outside = tmp_path / "outside"
    outside_task = outside / str(TASK_ID)
    outside_task.mkdir(parents=True)
    marker = outside_task / "marker.txt"
    marker.write_text("keep", encoding="utf-8")
    original_rename = os.rename
    original_replace = os.replace
    attack_attempted = False
    attack_blocked = False
    root_swapped = False

    def attempt_root_swap(source: os.PathLike[str] | str) -> None:
        nonlocal attack_attempted, attack_blocked, root_swapped
        if attack_attempted or Path(source).name != str(TASK_ID):
            return
        attack_attempted = True
        try:
            original_rename(staging_root, displaced_root)
            os.symlink(outside, staging_root, target_is_directory=True)
        except OSError:
            attack_blocked = True
        else:
            root_swapped = True

    def rename_with_root_swap(
        source: os.PathLike[str] | str,
        destination: os.PathLike[str] | str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        attempt_root_swap(source)
        original_rename(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    def replace_with_root_swap(
        source: os.PathLike[str] | str,
        destination: os.PathLike[str] | str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        attempt_root_swap(source)
        original_replace(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(os, "rename", rename_with_root_swap)
    monkeypatch.setattr(os, "replace", replace_with_root_swap)

    operation_rejected = False
    try:
        layout.cleanup()
    except ValueError:
        operation_rejected = True

    assert attack_attempted
    assert attack_blocked or (root_swapped and operation_rejected)
    assert marker.read_text(encoding="utf-8") == "keep"
    assert not (displaced_root / str(TASK_ID)).exists()

    if attack_blocked:
        monkeypatch.undo()
        staging_root.rename(displaced_root)
        assert displaced_root.is_dir()


def test_task_id_must_be_uuid(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="UUID"):
        StagingLayout.for_task(
            tmp_path / "staging",
            "../outside",  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
        )
