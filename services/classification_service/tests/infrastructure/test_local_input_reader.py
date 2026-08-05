from pathlib import Path

import pytest

from classification_service.infrastructure.input.local_text_reader import (
    InvalidInputUri,
    LocalTextReader,
)


def test_reader_reads_utf8_file_below_root(tmp_path: Path) -> None:
    source = tmp_path / "task" / "input.txt"
    source.parent.mkdir()
    source.write_text("中文文本", encoding="utf-8")

    assert LocalTextReader(tmp_path, max_input_bytes=1024).read(source.as_uri()) == "中文文本"


def test_reader_rejects_path_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    with pytest.raises(InvalidInputUri):
        LocalTextReader(root, max_input_bytes=1024).read(outside.as_uri())


def test_reader_rejects_oversized_and_non_utf8_input(tmp_path: Path) -> None:
    oversized = tmp_path / "oversized.txt"
    oversized.write_bytes(b"12345")
    invalid = tmp_path / "invalid.txt"
    invalid.write_bytes(b"\xff")
    reader = LocalTextReader(tmp_path, max_input_bytes=4)

    with pytest.raises(InvalidInputUri):
        reader.read(oversized.as_uri())
    with pytest.raises(InvalidInputUri):
        LocalTextReader(tmp_path, max_input_bytes=4).read(invalid.as_uri())
