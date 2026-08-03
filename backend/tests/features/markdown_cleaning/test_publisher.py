import hashlib
import os
import time
from multiprocessing import get_context
from multiprocessing.queues import Queue
from pathlib import Path

import pytest

from app.features.markdown_cleaning.processors.errors import (
    MarkdownCleaningErrorCode,
    MarkdownCleaningProcessorError,
)
from app.features.markdown_cleaning.publisher import (
    MarkdownCleaningResultPublisher,
    PreparedMarkdownResult,
)


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

    with pytest.raises(MarkdownCleaningProcessorError) as error:
        publisher.publish(prepared, target, allow_recovery=False)

    assert error.value.code is MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT


def test_publish_rejects_relative_target_even_if_filename_is_markdown(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.md"
    source.write_text("ok", encoding="utf-8")
    publisher = MarkdownCleaningResultPublisher(output_roots=(tmp_path,))
    prepared = publisher.prepare(source)

    with pytest.raises(MarkdownCleaningProcessorError) as error:
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

    with pytest.raises(MarkdownCleaningProcessorError) as error:
        publisher.publish(prepared, target, allow_recovery=False)

    assert error.value.code is MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT
    assert "已存在" in error.value.safe_message


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

    def _unsupported_link(_: Path, __: Path) -> None:
        raise OSError("hardlink unsupported")

    monkeypatch.setattr(os, "link", _unsupported_link)

    with pytest.raises(MarkdownCleaningProcessorError) as error:
        publisher.publish(prepared, target, allow_recovery=False)

    assert error.value.code is MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT


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


def test_publish_rejects_resolved_target_outside_output_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.md"
    source.write_text("ok", encoding="utf-8")
    output_root = tmp_path / "output"
    outside = tmp_path / "outside" / "result.md"
    output_root.mkdir()
    output_root_provider = output_root / "result.md"
    publisher = MarkdownCleaningResultPublisher(output_roots=(output_root,))
    prepared = publisher.prepare(source)
    original_resolve = Path.resolve

    def fake_resolve(self: Path, strict: bool = False) -> Path:
        if self == output_root_provider:
            return outside
        return original_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", fake_resolve)

    with pytest.raises(MarkdownCleaningProcessorError) as error:
        publisher.publish(prepared, output_root_provider, allow_recovery=False)

    assert error.value.code is MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT
    assert "不在允许输出目录" in error.value.safe_message
    assert "outside" not in str(error.value)
