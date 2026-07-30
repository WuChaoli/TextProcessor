from dataclasses import dataclass
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from app.features.structured_extraction.errors import (
    ExtractionErrorCode,
    ExtractionProcessingError,
)
from app.features.structured_extraction.safe_archive import (
    invalid_archive,
    read_safe_xml,
    validated_archive_entries,
)
from app.features.structured_extraction.worker_models import DetectedFormat

_OLE_MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")
_TEXT_SAMPLE_BYTES = 64 * 1024
_EXPLICITLY_UNSUPPORTED = {".wps", ".et", ".dps", ".ofd"}
_TEXT_EXTENSIONS = {
    ".txt": DetectedFormat.TEXT,
    ".log": DetectedFormat.TEXT,
    ".md": DetectedFormat.MARKDOWN,
    ".markdown": DetectedFormat.MARKDOWN,
    ".json": DetectedFormat.JSON,
    ".xml": DetectedFormat.XML,
    ".yaml": DetectedFormat.YAML,
    ".yml": DetectedFormat.YAML,
    ".csv": DetectedFormat.CSV,
    ".tsv": DetectedFormat.TSV,
}
_BINARY_EXTENSIONS = {
    ".pdf": DetectedFormat.PDF,
    ".doc": DetectedFormat.DOC,
    ".docx": DetectedFormat.DOCX,
    ".ppt": DetectedFormat.PPT,
    ".pptx": DetectedFormat.PPTX,
    ".xls": DetectedFormat.XLS,
    ".xlsx": DetectedFormat.XLSX,
    ".epub": DetectedFormat.EPUB,
    ".png": DetectedFormat.IMAGE,
    ".jpg": DetectedFormat.IMAGE,
    ".jpeg": DetectedFormat.IMAGE,
}


@dataclass(frozen=True)
class DetectedDocument:
    path: Path
    detected_format: DetectedFormat
    extension: str


class FormatDetector:
    def detect(self, path: Path) -> DetectedDocument:
        extension = path.suffix.lower()
        if extension in _EXPLICITLY_UNSUPPORTED:
            raise unsupported_format()
        try:
            with path.open("rb") as source:
                sample = source.read(_TEXT_SAMPLE_BYTES)
        except OSError:
            raise ExtractionProcessingError(
                ExtractionErrorCode.INPUT_ACCESS_FAILED,
                "无法读取输入文件",
            ) from None
        detected = self._detect_bytes(path, sample, extension)
        expected = _BINARY_EXTENSIONS.get(extension)
        if expected is not None and expected is not detected:
            raise unsupported_format()
        if extension in _TEXT_EXTENSIONS and detected not in {
            _TEXT_EXTENSIONS[extension],
            DetectedFormat.HTML,
        }:
            raise unsupported_format()
        return DetectedDocument(
            path=path,
            detected_format=detected,
            extension=extension,
        )

    def _detect_bytes(
        self,
        path: Path,
        sample: bytes,
        extension: str,
    ) -> DetectedFormat:
        if sample.startswith(b"%PDF-"):
            return DetectedFormat.PDF
        if sample.startswith(b"\x89PNG\r\n\x1a\n") or sample.startswith(
            b"\xff\xd8\xff"
        ):
            return DetectedFormat.IMAGE
        if sample.startswith(_OLE_MAGIC):
            ole_formats = {
                ".doc": DetectedFormat.DOC,
                ".ppt": DetectedFormat.PPT,
                ".xls": DetectedFormat.XLS,
            }
            try:
                return ole_formats[extension]
            except KeyError:
                raise unsupported_format() from None
        if sample.startswith(b"PK\x03\x04"):
            return self._detect_zip(path, extension)
        return self._detect_text(sample, extension)

    def _detect_zip(self, path: Path, extension: str) -> DetectedFormat:
        try:
            with ZipFile(path) as package:
                entries = validated_archive_entries(package)
                if "mimetype" in entries:
                    mimetype = package.read(entries["mimetype"])
                    if mimetype == b"application/epub+zip":
                        return DetectedFormat.EPUB
                if "[Content_Types].xml" not in entries:
                    raise invalid_archive()
                read_safe_xml(package, entries, "[Content_Types].xml")
                structures = {
                    "word/document.xml": DetectedFormat.DOCX,
                    "ppt/presentation.xml": DetectedFormat.PPTX,
                    "xl/workbook.xml": DetectedFormat.XLSX,
                }
                matches = {
                    detected
                    for member, detected in structures.items()
                    if member in entries
                }
                if len(matches) != 1:
                    raise invalid_archive()
                detected = matches.pop()
                expected = _BINARY_EXTENSIONS.get(extension)
                if expected is not detected:
                    raise unsupported_format()
                return detected
        except ExtractionProcessingError:
            raise
        except BadZipFile, OSError:
            raise invalid_archive() from None

    @staticmethod
    def _detect_text(sample: bytes, extension: str) -> DetectedFormat:
        if b"\0" in sample or any(byte < 9 or 13 < byte < 32 for byte in sample):
            raise unsupported_format()
        decoded = None
        for encoding in ("utf-8-sig", "gb18030"):
            try:
                decoded = sample.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if decoded is None:
            raise unsupported_format()
        normalized = decoded.lstrip().lower()
        if normalized.startswith(("<!doctype html", "<html", "<head", "<body")):
            return DetectedFormat.HTML
        return _TEXT_EXTENSIONS.get(extension, DetectedFormat.UNKNOWN_TEXT)


def unsupported_format() -> ExtractionProcessingError:
    return ExtractionProcessingError(
        ExtractionErrorCode.UNSUPPORTED_INPUT_FORMAT,
        "输入格式不受支持或与文件特征冲突",
    )
