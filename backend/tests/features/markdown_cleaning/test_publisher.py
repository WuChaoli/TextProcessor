import hashlib
import logging
import os
import time
from multiprocessing import get_context
from multiprocessing.queues import Queue
from pathlib import Path

import pytest

import app.features.markdown_cleaning.publisher as publisher_module
from app.features.markdown_cleaning.processors.errors import (
    MarkdownCleaningErrorCode,
    MarkdownCleaningProcessorError,
)
from app.features.markdown_cleaning.publisher import (
    InvalidPreparedOutputError,
    MarkdownCleaningResultPublisher,
    OutputConflictError,
    PreparedMarkdownResult,
    PublicationSystemError,
)


@pytest.fixture(scope="session", autouse=True)
def db() -> None:
    """Override the backend-wide PostgreSQL fixture for this pure unit module."""


def _publish_worker(
    source: str,
    target: str,
    output_root: str,
    ready_file: str,
    queue: Queue[str],
) -> None:
    publisher = MarkdownCleaningResultPublisher(
        output_roots=(Path(output_root),),
        max_output_bytes=1024,
    )
    ready = Path(ready_file)
    while not ready.exists():
        time.sleep(0.01)
    prepared = publisher.prepare(Path(source))
    try:
        publisher.publish(prepared, Path(target), allow_recovery=False)
        queue.put("published")
    except MarkdownCleaningProcessorError as error:
        queue.put(error.code.value)


def test_prepare_validates_utf8_and_records_digest_and_size(tmp_path: Path) -> None:
    publisher = MarkdownCleaningResultPublisher(
        output_roots=(tmp_path,),
        max_output_bytes=1024,
    )
    source = tmp_path / "source.md"
    source.write_text("abc", encoding="utf-8")

    prepared = publisher.prepare(source)

    assert prepared.sha256 == hashlib.sha256(b"abc").hexdigest()
    assert prepared.size_bytes == 3


def test_prepare_rejects_invalid_utf8(tmp_path: Path) -> None:
    publisher = MarkdownCleaningResultPublisher(
        output_roots=(tmp_path,),
        max_output_bytes=1024,
    )
    source = tmp_path / "source.md"
    source.write_bytes(b"abc\xff")

    with pytest.raises(MarkdownCleaningProcessorError) as error:
        publisher.prepare(source)

    assert error.value.code is MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT
    assert "非法 UTF-8" in error.value.safe_message


def test_publish_rejects_target_outside_allowed_output_root(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.md"
    source.write_text("ok", encoding="utf-8")
    publisher = MarkdownCleaningResultPublisher(output_roots=(tmp_path / "output",))
    prepared = publisher.prepare(source)
    outside = tmp_path / "outside" / "result.md"

    with pytest.raises(MarkdownCleaningProcessorError) as error:
        publisher.publish(prepared, outside, allow_recovery=False)

    assert error.value.code is MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT
    assert "outside" not in str(error.value)


def test_publish_rejects_non_markdown_target_path(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.md"
    source.write_text("ok", encoding="utf-8")
    publisher = MarkdownCleaningResultPublisher(output_roots=(tmp_path,))
    prepared = publisher.prepare(source)
    target = tmp_path / "result.txt"

    with pytest.raises(InvalidPreparedOutputError) as error:
        publisher.publish(prepared, target, allow_recovery=False)

    assert error.value.code is MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT


def test_publish_rejects_relative_target_even_if_filename_is_markdown(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.md"
    source.write_text("ok", encoding="utf-8")
    publisher = MarkdownCleaningResultPublisher(output_roots=(tmp_path,))
    prepared = publisher.prepare(source)

    with pytest.raises(InvalidPreparedOutputError) as error:
        publisher.publish(prepared, Path("result.md"), allow_recovery=False)

    assert error.value.code is MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT
    assert "路径不在允许输出目录" in error.value.safe_message


def test_publish_rejects_existing_target_without_recovery(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.md"
    source.write_text("new", encoding="utf-8")
    target = tmp_path / "output" / "result.md"
    target.parent.mkdir()
    target.write_text("existing", encoding="utf-8")

    publisher = MarkdownCleaningResultPublisher(output_roots=(tmp_path / "output",))
    prepared = publisher.prepare(source)

    with pytest.raises(OutputConflictError) as error:
        publisher.publish(prepared, target, allow_recovery=False)

    assert error.value.code is MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT
    assert "已存在" in error.value.safe_message


def test_prepare_invalid_utf8_has_distinct_deterministic_type(tmp_path: Path) -> None:
    source = tmp_path / "bad.md"
    source.write_bytes(b"\xff")
    publisher = MarkdownCleaningResultPublisher(output_roots=(tmp_path,))
    with pytest.raises(InvalidPreparedOutputError):
        publisher.prepare(source)


def test_publish_filesystem_failure_has_retryable_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.md"
    source.write_text("ok", encoding="utf-8")
    publisher = MarkdownCleaningResultPublisher(output_roots=(tmp_path,))
    prepared = publisher.prepare(source)

    def fail_link(*_args: object) -> None:
        raise OSError("io")

    monkeypatch.setattr(publisher, "_link_no_replace", fail_link)
    with pytest.raises(PublicationSystemError):
        publisher.publish(prepared, tmp_path / "out.md", allow_recovery=False)

    assert not list(tmp_path.glob(".markdown-cleaning-publish-*.tmp"))


def test_publish_success_removes_temporary_entry(tmp_path: Path) -> None:
    source = tmp_path / "source.md"
    source.write_text("ok", encoding="utf-8")
    output = tmp_path / "output"
    output.mkdir()
    publisher = MarkdownCleaningResultPublisher(output_roots=(output,))

    publisher.publish(
        publisher.prepare(source), output / "result.md", allow_recovery=False
    )

    assert [path.name for path in output.iterdir()] == ["result.md"]


def test_publish_success_is_not_reversed_when_temporary_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    source = tmp_path / "source.md"
    source.write_text("published", encoding="utf-8")
    output = tmp_path / "output"
    output.mkdir()
    target = output / "result.md"
    publisher = MarkdownCleaningResultPublisher(output_roots=(output,))

    def fail_cleanup(*_args: object, **_kwargs: object) -> None:
        raise OSError("sensitive cleanup failure", str(tmp_path / "secret.md"))

    if os.name == "nt":
        monkeypatch.setattr(
            publisher_module._WindowsPinnedDirectory,
            "delete_relative",
            fail_cleanup,
        )
    else:
        monkeypatch.setattr(publisher_module.os, "unlink", fail_cleanup)
    with caplog.at_level(logging.WARNING):
        result = publisher.publish(
            publisher.prepare(source), target, allow_recovery=False
        )

    assert result.path == target
    assert target.read_bytes() == b"published"
    assert "markdown cleaning publish temporary cleanup failed" in caplog.text
    assert caplog.records[-1].cleanup_stage == "temporary"  # type: ignore[attr-defined]
    assert str(tmp_path) not in caplog.text


def test_publish_recovers_only_when_digest_and_size_match(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.md"
    source.write_text("same", encoding="utf-8")
    target = tmp_path / "result.md"
    target.write_text("same", encoding="utf-8")

    publisher = MarkdownCleaningResultPublisher(output_roots=(tmp_path,))
    prepared = publisher.prepare(source)

    recovered = publisher.publish(prepared, target, allow_recovery=True)
    assert recovered.recovered is True
    assert target.read_text(encoding="utf-8") == "same"

    target.write_text("different", encoding="utf-8")
    with pytest.raises(MarkdownCleaningProcessorError) as error:
        publisher.publish(prepared, target, allow_recovery=True)

    assert error.value.code is MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT


def test_publish_rejects_prepared_mismatch_even_if_target_same_size(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.md"
    source.write_text("same", encoding="utf-8")
    target = tmp_path / "result.md"
    target.write_text("same", encoding="utf-8")

    publisher = MarkdownCleaningResultPublisher(output_roots=(tmp_path,))
    prepared = publisher.prepare(source)
    mismatched = PreparedMarkdownResult(
        path=prepared.path,
        sha256="0" * 64,
        size_bytes=prepared.size_bytes,
    )

    with pytest.raises(MarkdownCleaningProcessorError) as error:
        publisher.publish(mismatched, target, allow_recovery=True)

    assert error.value.code is MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT


def test_publish_recovers_without_overwrite_with_multiprocess_race(
    tmp_path: Path,
) -> None:
    source_a = tmp_path / "source-a.md"
    source_b = tmp_path / "source-b.md"
    source_a.write_text("content-a", encoding="utf-8")
    source_b.write_text("content-b", encoding="utf-8")
    probe = tmp_path / "link-probe.md"
    probe_target = tmp_path / "link-target.md"
    probe.write_text("x", encoding="utf-8")
    try:
        os.link(probe, probe_target)
        probe_target.unlink()
    except OSError as exc:
        pytest.skip(f"hardlink unavailable for cross-process check: {exc}")
    target = tmp_path / "out" / "result.md"
    output_root = tmp_path / "out"
    output_root.mkdir()
    publisher = MarkdownCleaningResultPublisher(output_roots=(output_root,))
    prepared_a = publisher.prepare(source_a)
    prepared_b = publisher.prepare(source_b)
    context = get_context("spawn")
    queue: Queue[str] = context.Queue()
    ready_file = tmp_path / "publish-go"
    process_a = context.Process(
        target=_publish_worker,
        args=(
            str(prepared_a.path),
            str(target),
            str(output_root),
            str(ready_file),
            queue,
        ),
    )
    process_b = context.Process(
        target=_publish_worker,
        args=(
            str(prepared_b.path),
            str(target),
            str(output_root),
            str(ready_file),
            queue,
        ),
    )
    process_a.start()
    process_b.start()
    ready_file.touch()
    process_a.join(timeout=30)
    process_b.join(timeout=30)
    assert process_a.exitcode == 0
    assert process_b.exitcode == 0

    assert not process_a.is_alive()
    assert not process_b.is_alive()
    outcomes = [queue.get(timeout=5), queue.get(timeout=5)]
    assert sorted(outcomes) == [
        MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT.value,
        "published",
    ]
    assert target.read_text(encoding="utf-8") in {"content-a", "content-b"}


def test_publish_fails_when_hardlink_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.md"
    source.write_text("ok", encoding="utf-8")
    publisher = MarkdownCleaningResultPublisher(output_roots=(tmp_path,))
    prepared = publisher.prepare(source)
    target = tmp_path / "result.md"

    def _unsupported_link(*_args: object, **_kwargs: object) -> None:
        raise OSError("hardlink unsupported")

    if os.name == "nt":
        monkeypatch.setattr(
            publisher_module._WindowsPinnedDirectory,
            "link_no_replace",
            _unsupported_link,
        )
    else:
        monkeypatch.setattr(os, "link", _unsupported_link)

    with pytest.raises(PublicationSystemError) as error:
        publisher.publish(prepared, target, allow_recovery=False)

    assert error.value.code is MarkdownCleaningErrorCode.INTERNAL_ERROR


def test_publish_uses_directory_fd_for_hardlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.md"
    source.write_text("ok", encoding="utf-8")
    publisher = MarkdownCleaningResultPublisher(output_roots=(tmp_path,))
    prepared = publisher.prepare(source)
    original_link = os.link
    observed = {"src_dir_fd": None, "dst_dir_fd": None}

    def _spy_link(
        source_name: str,
        target_name: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        observed["src_dir_fd"] = src_dir_fd
        observed["dst_dir_fd"] = dst_dir_fd
        return original_link(
            source_name,
            target_name,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(os, "link", _spy_link)

    result = publisher.publish(prepared, tmp_path / "result.md", allow_recovery=False)

    assert result.path == tmp_path / "result.md"
    if os.name != "nt":
        assert observed["src_dir_fd"] is not None
        assert observed["dst_dir_fd"] is not None


def test_publish_calls_fsync_for_output_and_parent(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.md"
    source.write_text("ok", encoding="utf-8")
    publisher = MarkdownCleaningResultPublisher(output_roots=(tmp_path,))
    prepared = publisher.prepare(source)
    calls = []

    def _fake_fsync(descriptor: int) -> None:
        calls.append(descriptor)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(publisher_module.os, "fsync", _fake_fsync)
    try:
        publisher.publish(prepared, tmp_path / "result.md", allow_recovery=False)
    finally:
        monkeypatch.undo()

    assert len(calls) == (1 if os.name == "nt" else 2)


def test_publish_rejects_target_escape_via_output_symlink(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.md"
    source.write_text("ok", encoding="utf-8")
    output_root = tmp_path / "output"
    outside_root = tmp_path / "outside"
    output_root.mkdir()
    outside_root.mkdir()
    publisher = MarkdownCleaningResultPublisher(output_roots=(output_root,))
    prepared = publisher.prepare(source)
    linked = output_root / "linked"
    try:
        linked.symlink_to(outside_root, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"creating an output symlink is unavailable: {exc}")

    with pytest.raises(MarkdownCleaningProcessorError) as error:
        publisher.publish(prepared, linked / "result.md", allow_recovery=False)

    assert error.value.code is MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT
    assert "不在允许输出目录" in error.value.safe_message


def test_publish_rejects_target_parent_open_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.md"
    source.write_text("ok", encoding="utf-8")
    output_root = tmp_path / "output"
    output_root.mkdir()
    target = output_root / "result.md"
    publisher = MarkdownCleaningResultPublisher(output_roots=(output_root,))
    prepared = publisher.prepare(source)
    original_open_directory = (
        publisher_module.MarkdownCleaningResultPublisher._open_directory_no_follow
    )

    def _open_directory_no_follow(path: Path) -> int:
        if path == output_root:
            raise OSError("output root swapped")
        return original_open_directory(path)

    monkeypatch.setattr(
        publisher_module.MarkdownCleaningResultPublisher,
        "_open_directory_no_follow",
        _open_directory_no_follow,
    )

    with pytest.raises(PublicationSystemError) as error:
        publisher.publish(prepared, target, allow_recovery=False)

    assert error.value.code is MarkdownCleaningErrorCode.INTERNAL_ERROR


def test_publish_propagates_unexpected_fsync_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.md"
    source.write_text("ok", encoding="utf-8")
    publisher = MarkdownCleaningResultPublisher(output_roots=(tmp_path,))
    prepared = publisher.prepare(source)

    def _fail_fsync(_descriptor: int) -> None:
        raise OSError(5, "unexpected fsync failure")

    monkeypatch.setattr(publisher_module.os, "fsync", _fail_fsync)
    with pytest.raises(PublicationSystemError):
        publisher.publish(prepared, tmp_path / "result.md", allow_recovery=False)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction race regression")
def test_publish_pins_parent_when_path_is_swapped_to_junction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.md"
    source.write_text("safe", encoding="utf-8")
    output = tmp_path / "output"
    pinned = tmp_path / "pinned"
    outside = tmp_path / "outside"
    output.mkdir()
    outside.mkdir()
    publisher = MarkdownCleaningResultPublisher(output_roots=(output,))
    prepared = publisher.prepare(source)
    original_copy = publisher._copy_to_exclusive_temporary

    def _swap_then_copy(*args: object, **kwargs: object) -> int:
        output.rename(pinned)
        import subprocess

        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(output), str(outside)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            pytest.skip("junction creation unavailable")
        return original_copy(*args, **kwargs)

    monkeypatch.setattr(publisher, "_copy_to_exclusive_temporary", _swap_then_copy)
    published = publisher.publish(prepared, output / "result.md", allow_recovery=False)

    assert published.sha256 == prepared.sha256
    assert (pinned / "result.md").read_text(encoding="utf-8") == "safe"
    assert not (outside / "result.md").exists()
    assert not list(outside.iterdir())


def test_publish_rejects_source_tampering_after_prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.md"
    source.write_text("safe", encoding="utf-8")
    output = tmp_path / "output"
    output.mkdir()
    publisher = MarkdownCleaningResultPublisher(output_roots=(output,))
    prepared = publisher.prepare(source)
    original_copy = publisher._copy_to_exclusive_temporary

    def _tamper_then_copy(*args: object, **kwargs: object) -> int:
        source.write_text("evil", encoding="utf-8")
        return original_copy(*args, **kwargs)

    monkeypatch.setattr(publisher, "_copy_to_exclusive_temporary", _tamper_then_copy)
    with pytest.raises(InvalidPreparedOutputError) as error:
        publisher.publish(prepared, output / "result.md", allow_recovery=False)

    assert error.value.code is MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT
    assert "摘要" in error.value.safe_message
    assert not (output / "result.md").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction race regression")
def test_publish_pins_every_ancestor_during_relative_descent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    source = tmp_path / "source.md"
    source.write_text("safe", encoding="utf-8")
    root = tmp_path / "root"
    ancestor = root / "a"
    moved = root / "a-pinned"
    outside = tmp_path / "outside"
    ancestor.mkdir(parents=True)
    outside.mkdir()
    publisher = MarkdownCleaningResultPublisher(output_roots=(root,))
    prepared = publisher.prepare(source)
    original_open_child = publisher._open_child_directory

    def _swap_a_before_opening_b(parent_handle: int, name: str) -> int:
        if name == "b" and not moved.exists():
            ancestor.rename(moved)
            completed = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(ancestor), str(outside)],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                pytest.skip("junction creation unavailable")
        return original_open_child(parent_handle, name)

    monkeypatch.setattr(publisher, "_open_child_directory", _swap_a_before_opening_b)
    publisher.publish(prepared, root / "a" / "b" / "result.md", allow_recovery=False)

    assert (moved / "b" / "result.md").read_text(encoding="utf-8") == "safe"
    assert not list(outside.iterdir())


@pytest.mark.skipif(os.name != "nt", reason="Windows junction race regression")
def test_publish_rejects_junction_swapped_before_first_descendant_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    source = tmp_path / "source.md"
    source.write_text("safe", encoding="utf-8")
    root = tmp_path / "root"
    ancestor = root / "a"
    moved = root / "a-original"
    outside = tmp_path / "outside"
    ancestor.mkdir(parents=True)
    outside.mkdir()
    publisher = MarkdownCleaningResultPublisher(output_roots=(root,))
    prepared = publisher.prepare(source)
    original_open_child = publisher._open_child_directory

    def _swap_before_opening_a(parent_handle: int, name: str) -> int:
        if name == "a" and not moved.exists():
            ancestor.rename(moved)
            completed = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(ancestor), str(outside)],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                pytest.skip("junction creation unavailable")
        return original_open_child(parent_handle, name)

    monkeypatch.setattr(publisher, "_open_child_directory", _swap_before_opening_a)
    with pytest.raises(PublicationSystemError) as error:
        publisher.publish(
            prepared, root / "a" / "b" / "result.md", allow_recovery=False
        )

    assert error.value.code is MarkdownCleaningErrorCode.INTERNAL_ERROR
    assert not list(outside.iterdir())
    assert not (moved / "b").exists()
