import stat
from dataclasses import dataclass
from pathlib import Path

from app.features.global_deduplication.errors import (
    GlobalDeduplicationErrorCode,
    GlobalDeduplicationProcessingError,
)
from app.features.global_deduplication.models import (
    DocumentReference,
    NormalizedDocument,
)

_SUPPORTED_SUFFIXES = frozenset({".md", ".txt", ".json"})


@dataclass(frozen=True, slots=True)
class ScannedDocument:
    relative_path: Path
    absolute_path: Path


@dataclass(frozen=True, slots=True)
class ScannedBatch:
    root: Path
    original_root: Path
    duplicate_root: Path
    documents: tuple[ScannedDocument, ...]


def scan_batch(batch_path: Path) -> ScannedBatch:
    root = batch_path.resolve(strict=True)
    original = root / "original"
    duplicate = root / "duplicate"
    try:
        if not root.is_dir() or not original.is_dir() or not duplicate.is_dir():
            raise OSError
        documents = tuple(
            sorted(
                (
                    ScannedDocument(path.relative_to(original), path)
                    for path in original.rglob("*")
                    if _is_supported_regular_file(path)
                ),
                key=lambda item: item.relative_path.as_posix(),
            )
        )
    except OSError, RuntimeError:
        raise GlobalDeduplicationProcessingError(
            GlobalDeduplicationErrorCode.INPUT_MANIFEST_ACCESS_FAILED,
            "输入目录不可访问",
        ) from None
    if not documents:
        raise GlobalDeduplicationProcessingError(
            GlobalDeduplicationErrorCode.EMPTY_DOCUMENT_LIST,
            "输入目录中没有可处理文档",
        )
    return ScannedBatch(root, original, duplicate, documents)


def load_scanned_documents(
    scanned: ScannedBatch,
    *,
    max_document_bytes: int,
    max_total_bytes: int,
) -> tuple[NormalizedDocument, ...]:
    total_bytes = 0
    documents: list[NormalizedDocument] = []
    for item in scanned.documents:
        try:
            content = item.absolute_path.read_bytes()
        except OSError:
            raise GlobalDeduplicationProcessingError(
                GlobalDeduplicationErrorCode.DOCUMENT_READ_FAILED,
                "文档读取失败",
            ) from None
        if len(content) > max_document_bytes:
            raise GlobalDeduplicationProcessingError(
                GlobalDeduplicationErrorCode.DOCUMENT_TOO_LARGE,
                "文档超过大小限制",
            )
        total_bytes += len(content)
        if total_bytes > max_total_bytes:
            raise GlobalDeduplicationProcessingError(
                GlobalDeduplicationErrorCode.BATCH_TOO_LARGE,
                "批次文档累计大小超过限制",
            )
        try:
            text = content.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
        except UnicodeDecodeError:
            raise GlobalDeduplicationProcessingError(
                GlobalDeduplicationErrorCode.DOCUMENT_DECODE_FAILED,
                "文档不是有效的 UTF-8 文本",
            ) from None
        documents.append(
            NormalizedDocument(
                reference=DocumentReference(
                    file_id=item.relative_path.as_posix(),
                    file_storage_path=str(item.absolute_path),
                ),
                text=text,
                size_bytes=len(content),
            )
        )
    return tuple(documents)


def _is_supported_regular_file(path: Path) -> bool:
    try:
        details = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(details.st_mode) and path.suffix.lower() in _SUPPORTED_SUFFIXES
