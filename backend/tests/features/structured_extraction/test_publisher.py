import hashlib
import threading
from pathlib import Path

import pytest

from app.features.structured_extraction.errors import (
    ExtractionErrorCode,
    ExtractionProcessingError,
)
from app.features.structured_extraction.processors.publisher import AtomicPublisher


@pytest.fixture(autouse=True)
def db() -> None:
    """发布器单测不依赖 PostgreSQL。"""


def test_prepare_validates_utf8_and_computes_digest(tmp_path: Path) -> None:
    source = tmp_path / "result.md"
    content = "# 结果\n".encode()
    source.write_bytes(content)

    prepared = AtomicPublisher(
        max_output_bytes=1024,
        output_roots=(tmp_path,),
    ).prepare(source)

    assert prepared.sha256 == hashlib.sha256(content).hexdigest()
    assert prepared.size_bytes == len(content)


def test_publish_rejects_preexisting_target_without_overwriting(
    tmp_path: Path,
) -> None:
    source = tmp_path / "staging" / "result.md"
    source.parent.mkdir()
    source.write_text("new", encoding="utf-8")
    target = tmp_path / "output" / "result.md"
    target.parent.mkdir()
    target.write_text("existing", encoding="utf-8")
    publisher = AtomicPublisher(
        max_output_bytes=1024,
        output_roots=(tmp_path / "output",),
    )

    with pytest.raises(ExtractionProcessingError) as captured:
        publisher.publish(publisher.prepare(source), target)

    assert captured.value.code is ExtractionErrorCode.OUTPUT_CONFLICT
    assert target.read_text(encoding="utf-8") == "existing"


def test_only_one_concurrent_publish_can_create_target(tmp_path: Path) -> None:
    sources = [tmp_path / f"source-{index}.md" for index in range(2)]
    for index, source in enumerate(sources):
        source.write_text(f"content-{index}", encoding="utf-8")
    target = tmp_path / "output" / "result.md"
    publisher = AtomicPublisher(
        max_output_bytes=1024,
        output_roots=(tmp_path / "output",),
    )
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def publish(source: Path) -> None:
        prepared = publisher.prepare(source)
        barrier.wait()
        try:
            publisher.publish(prepared, target)
            outcomes.append("published")
        except ExtractionProcessingError as error:
            outcomes.append(error.code.value)

    threads = [threading.Thread(target=publish, args=(source,)) for source in sources]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["OUTPUT_CONFLICT", "published"]
    assert target.read_text(encoding="utf-8") in {"content-0", "content-1"}


def test_recovery_accepts_same_digest_and_rejects_different_digest(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.md"
    source.write_text("same", encoding="utf-8")
    target = tmp_path / "target.md"
    target.write_text("same", encoding="utf-8")
    publisher = AtomicPublisher(
        max_output_bytes=1024,
        output_roots=(tmp_path,),
    )
    prepared = publisher.prepare(source)

    recovered = publisher.publish(prepared, target, allow_recovery=True)

    assert recovered.recovered is True
    target.write_text("different", encoding="utf-8")
    with pytest.raises(ExtractionProcessingError) as captured:
        publisher.publish(prepared, target, allow_recovery=True)
    assert captured.value.code is ExtractionErrorCode.OUTPUT_CONFLICT


def test_publish_uses_only_target_directory_for_temporary_file(
    tmp_path: Path,
) -> None:
    source = tmp_path / "staging" / "result.md"
    source.parent.mkdir()
    source.write_text("content", encoding="utf-8")
    target = tmp_path / "output" / "result.md"
    publisher = AtomicPublisher(
        max_output_bytes=1024,
        output_roots=(tmp_path / "output",),
    )

    publisher.publish(publisher.prepare(source), target)

    assert not list(source.parent.glob(".publish-*"))
    assert not list(target.parent.glob(".publish-*"))


def test_publish_manifest_atomically_and_recovers_same_content(tmp_path: Path) -> None:
    source = tmp_path / "staging" / "manifest.json"
    source.parent.mkdir()
    source.write_text('{"schemaVersion":1}\n', encoding="utf-8")
    target = tmp_path / "output" / "manifest.json"
    publisher = AtomicPublisher(
        max_output_bytes=1024,
        output_roots=(tmp_path / "output",),
    )
    prepared = publisher.prepare(source)

    published = publisher.publish_manifest(prepared, target)
    recovered = publisher.publish_manifest(prepared, target, allow_recovery=True)

    assert published.recovered is False
    assert recovered.recovered is True
    assert target.read_text(encoding="utf-8") == '{"schemaVersion":1}\n'
    assert not list(target.parent.glob(".publish-*"))


def test_target_is_unavailable_when_task_directory_has_manifest(tmp_path: Path) -> None:
    output = tmp_path / "output"
    output.mkdir()
    (output / "manifest.json").write_text("{}\n", encoding="utf-8")
    publisher = AtomicPublisher(max_output_bytes=1024, output_roots=(output,))

    with pytest.raises(ExtractionProcessingError) as captured:
        publisher.ensure_target_available(output / "another.md")

    assert captured.value.code is ExtractionErrorCode.OUTPUT_CONFLICT
