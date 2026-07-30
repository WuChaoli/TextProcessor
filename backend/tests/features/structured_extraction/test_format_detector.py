import zipfile
from pathlib import Path

import pytest

from app.features.structured_extraction.errors import (
    ExtractionErrorCode,
    ExtractionProcessingError,
)
from app.features.structured_extraction.format_detector import FormatDetector
from app.features.structured_extraction.worker_models import DetectedFormat


@pytest.mark.parametrize(
    ("filename", "content", "expected"),
    [
        ("sample.pdf", b"%PDF-1.7\n", DetectedFormat.PDF),
        ("sample.png", b"\x89PNG\r\n\x1a\nrest", DetectedFormat.IMAGE),
        ("sample.jpg", b"\xff\xd8\xff\xe0rest", DetectedFormat.IMAGE),
        ("sample.json", b'{"name": "value"}', DetectedFormat.JSON),
        ("sample.xml", b"<?xml version='1.0'?><root/>", DetectedFormat.XML),
        ("sample.html", b"<!doctype html><html></html>", DetectedFormat.HTML),
        ("sample.unknown", "通用文本".encode(), DetectedFormat.UNKNOWN_TEXT),
    ],
)
def test_detects_signature_and_text_formats(
    tmp_path: Path,
    filename: str,
    content: bytes,
    expected: DetectedFormat,
) -> None:
    path = tmp_path / filename
    path.write_bytes(content)

    document = FormatDetector().detect(path)

    assert document.detected_format is expected


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("sample.doc", DetectedFormat.DOC),
        ("sample.ppt", DetectedFormat.PPT),
        ("sample.xls", DetectedFormat.XLS),
    ],
)
def test_ole_container_uses_allowed_office_extension(
    tmp_path: Path,
    filename: str,
    expected: DetectedFormat,
) -> None:
    path = tmp_path / filename
    path.write_bytes(bytes.fromhex("D0CF11E0A1B11AE1") + b"container")

    assert FormatDetector().detect(path).detected_format is expected


@pytest.mark.parametrize(
    ("filename", "member", "expected"),
    [
        ("sample.docx", "word/document.xml", DetectedFormat.DOCX),
        ("sample.pptx", "ppt/presentation.xml", DetectedFormat.PPTX),
        ("sample.xlsx", "xl/workbook.xml", DetectedFormat.XLSX),
    ],
)
def test_detects_ooxml_from_package_structure(
    tmp_path: Path,
    filename: str,
    member: str,
    expected: DetectedFormat,
) -> None:
    path = tmp_path / filename
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("[Content_Types].xml", "<Types/>")
        package.writestr(member, "<root/>")

    assert FormatDetector().detect(path).detected_format is expected


def test_detects_epub_mimetype(tmp_path: Path) -> None:
    path = tmp_path / "sample.epub"
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("mimetype", "application/epub+zip")

    assert FormatDetector().detect(path).detected_format is DetectedFormat.EPUB


def test_rejects_extension_that_conflicts_with_binary_signature(
    tmp_path: Path,
) -> None:
    path = tmp_path / "disguised.txt"
    path.write_bytes(b"%PDF-1.7\n")

    with pytest.raises(ExtractionProcessingError) as captured:
        FormatDetector().detect(path)

    assert captured.value.code is ExtractionErrorCode.UNSUPPORTED_INPUT_FORMAT


def test_rejects_unknown_binary_with_nul(tmp_path: Path) -> None:
    path = tmp_path / "sample.bin"
    path.write_bytes(b"abc\x00def")

    with pytest.raises(ExtractionProcessingError) as captured:
        FormatDetector().detect(path)

    assert captured.value.code is ExtractionErrorCode.UNSUPPORTED_INPUT_FORMAT


def test_rejects_zip_path_escape(tmp_path: Path) -> None:
    path = tmp_path / "sample.docx"
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("[Content_Types].xml", "<Types/>")
        package.writestr("../word/document.xml", "<root/>")

    with pytest.raises(ExtractionProcessingError) as captured:
        FormatDetector().detect(path)

    assert captured.value.code is ExtractionErrorCode.UNSUPPORTED_INPUT_FORMAT
