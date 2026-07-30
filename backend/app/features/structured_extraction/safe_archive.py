from pathlib import PurePosixPath
from typing import cast
from xml.etree.ElementTree import Element
from zipfile import ZipFile, ZipInfo

from defusedxml import ElementTree as DefusedElementTree  # type: ignore[import-untyped]

from app.features.structured_extraction.errors import (
    ExtractionErrorCode,
    ExtractionProcessingError,
)

MAX_ARCHIVE_ENTRIES = 1000
MAX_ARCHIVE_ENTRY_BYTES = 10 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 50 * 1024 * 1024


def validated_archive_entries(package: ZipFile) -> dict[str, ZipInfo]:
    entries = package.infolist()
    if len(entries) > MAX_ARCHIVE_ENTRIES:
        raise invalid_archive()
    total_size = 0
    result: dict[str, ZipInfo] = {}
    for entry in entries:
        normalized_name = entry.filename.replace("\\", "/")
        path = PurePosixPath(normalized_name)
        if path.is_absolute() or ".." in path.parts:
            raise invalid_archive()
        if entry.file_size > MAX_ARCHIVE_ENTRY_BYTES:
            raise invalid_archive()
        total_size += entry.file_size
        if total_size > MAX_ARCHIVE_TOTAL_BYTES:
            raise invalid_archive()
        result[normalized_name] = entry
    return result


def read_safe_xml(
    package: ZipFile,
    entries: dict[str, ZipInfo],
    name: str,
) -> Element:
    entry = entries.get(name)
    if entry is None:
        raise invalid_archive()
    try:
        content = package.read(entry)
        return cast(Element, DefusedElementTree.fromstring(content))
    except Exception:
        raise invalid_archive() from None


def invalid_archive() -> ExtractionProcessingError:
    return ExtractionProcessingError(
        ExtractionErrorCode.UNSUPPORTED_INPUT_FORMAT,
        "压缩文档结构无效或不受支持",
    )
