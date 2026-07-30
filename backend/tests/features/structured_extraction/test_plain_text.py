import hashlib
from pathlib import Path

import pytest

from app.features.structured_extraction.errors import (
    ExtractionErrorCode,
    ExtractionProcessingError,
)
from app.features.structured_extraction.processors.plain_text import (
    PlainTextPassThroughProcessor,
)
from app.features.structured_extraction.worker_models import ProcessorName


@pytest.mark.parametrize(
    ("encoding", "content"),
    [
        ("utf-8", "第一行\n第二行"),
        ("utf-8-sig", "带 BOM 的文本\r\n第二行"),
        ("gb18030", "中文字段：保持原样\r\n"),
    ],
)
def test_plain_text_only_normalizes_encoding(
    tmp_path: Path,
    encoding: str,
    content: str,
) -> None:
    source = tmp_path / "source.txt"
    destination = tmp_path / "result.md"
    source.write_bytes(content.encode(encoding))
    processor = PlainTextPassThroughProcessor()

    artifact = processor.process(source, destination)

    assert destination.read_bytes() == content.encode("utf-8")
    assert artifact.processor_name is ProcessorName.PLAIN_TEXT
    assert artifact.markdown_path == destination
    assert (
        artifact.profile_sha256
        == hashlib.sha256(b'{"encodings":["utf-8-sig","gb18030"]}').hexdigest()
    )


@pytest.mark.parametrize(
    "content",
    [
        '{\n  "second": 2,\n  "first": 1\n}\n',
        "<root><second>2</second><first>1</first></root>\r\n",
    ],
)
def test_plain_text_does_not_reserialize_structured_text(
    tmp_path: Path,
    content: str,
) -> None:
    source = tmp_path / "source.txt"
    destination = tmp_path / "result.md"
    source.write_text(content, encoding="utf-8", newline="")

    PlainTextPassThroughProcessor().process(source, destination)

    with destination.open("r", encoding="utf-8", newline="") as output:
        assert output.read() == content


def test_plain_text_rejects_undecodable_input(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    destination = tmp_path / "result.md"
    source.write_bytes(b"\x81")

    with pytest.raises(ExtractionProcessingError) as captured:
        PlainTextPassThroughProcessor().process(source, destination)

    assert captured.value.code is ExtractionErrorCode.PROCESSING_FAILED
    assert not destination.exists()
