import json
import re
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import BinaryIO, Literal, Protocol
from urllib.parse import SplitResult, unquote, urljoin, urlsplit

import fsspec  # type: ignore[import-untyped]
import httpx

from app.features.global_deduplication.errors import (
    GlobalDeduplicationErrorCode,
    GlobalDeduplicationProcessingError,
)
from app.features.global_deduplication.models import (
    DocumentReference,
    NormalizedDocument,
)

_SUPPORTED_SUFFIXES = frozenset({".md", ".txt", ".json"})


def _input_error(
    code: GlobalDeduplicationErrorCode,
    message: str,
) -> GlobalDeduplicationProcessingError:
    return GlobalDeduplicationProcessingError(code, message)


def load_manifest_bytes(
    content: bytes,
    *,
    max_documents: int,
) -> tuple[DocumentReference, ...]:
    try:
        decoded = content.decode("utf-8-sig")
        value = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _input_error(
            GlobalDeduplicationErrorCode.INVALID_INPUT_MANIFEST,
            "输入清单不是有效的 UTF-8 JSON",
        ) from None
    if not isinstance(value, list):
        raise _input_error(
            GlobalDeduplicationErrorCode.INVALID_INPUT_MANIFEST,
            "输入清单顶层必须是数组",
        )
    if not value:
        raise _input_error(
            GlobalDeduplicationErrorCode.EMPTY_DOCUMENT_LIST,
            "输入清单不能为空",
        )
    if len(value) > max_documents:
        raise _input_error(
            GlobalDeduplicationErrorCode.INVALID_INPUT_MANIFEST,
            "输入清单文档数超过限制",
        )

    documents: list[DocumentReference] = []
    seen_file_ids: set[str] = set()
    for record in value:
        if not isinstance(record, dict):
            raise _input_error(
                GlobalDeduplicationErrorCode.INVALID_INPUT_MANIFEST,
                "输入清单记录必须是对象",
            )
        file_id = record.get("fileId")
        storage_path = record.get("fileStoragePath")
        if (
            not isinstance(file_id, str)
            or not file_id.strip()
            or not isinstance(storage_path, str)
            or not storage_path.strip()
        ):
            raise _input_error(
                GlobalDeduplicationErrorCode.INVALID_INPUT_MANIFEST,
                "输入清单缺少有效文件字段",
            )
        if file_id in seen_file_ids:
            raise _input_error(
                GlobalDeduplicationErrorCode.DUPLICATE_FILE_ID,
                "输入清单 fileId 重复",
            )
        seen_file_ids.add(file_id)
        documents.append(
            DocumentReference(
                file_id=file_id,
                file_storage_path=storage_path,
            )
        )
    return tuple(documents)


def normalize_document(content: bytes, *, suffix: str) -> str:
    if suffix.lower() not in _SUPPORTED_SUFFIXES:
        raise _input_error(
            GlobalDeduplicationErrorCode.UNSUPPORTED_DOCUMENT_FORMAT,
            "文档格式不受支持",
        )
    try:
        decoded = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise _input_error(
            GlobalDeduplicationErrorCode.DOCUMENT_DECODE_FAILED,
            "文档不是有效的 UTF-8 文本",
        ) from None
    return decoded.replace("\r\n", "\n").replace("\r", "\n")


class BoundedLocalReader:
    def __init__(
        self,
        *,
        input_roots: tuple[Path, ...],
        chunk_bytes: int,
    ) -> None:
        if not input_roots or chunk_bytes <= 0:
            raise ValueError("本地输入根目录和读取块大小必须有效")
        self._input_roots = tuple(
            root.resolve(strict=False) for root in input_roots
        )
        self._chunk_bytes = chunk_bytes

    def read(self, path: str | Path, *, max_bytes: int) -> bytes:
        if max_bytes <= 0:
            raise ValueError("读取大小限制必须为正数")
        normalized = Path(path).resolve(strict=False)
        if not any(
            normalized == root or root in normalized.parents
            for root in self._input_roots
        ):
            raise _input_error(
                GlobalDeduplicationErrorCode.DOCUMENT_PATH_NOT_ALLOWED,
                "文档路径不在允许范围内",
            )
        if not normalized.is_file():
            raise _input_error(
                GlobalDeduplicationErrorCode.DOCUMENT_NOT_FOUND,
                "文档不存在",
            )
        try:
            if normalized.stat().st_size > max_bytes:
                raise _input_error(
                    GlobalDeduplicationErrorCode.DOCUMENT_TOO_LARGE,
                    "文档超过大小限制",
                )
            with normalized.open("rb") as source:
                return self._read_bounded(source, max_bytes)
        except GlobalDeduplicationProcessingError:
            raise
        except OSError:
            raise _input_error(
                GlobalDeduplicationErrorCode.DOCUMENT_READ_FAILED,
                "文档读取失败",
            ) from None

    def _read_bounded(self, source: BinaryIO, max_bytes: int) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while chunk := source.read(self._chunk_bytes):
            total += len(chunk)
            if total > max_bytes:
                raise _input_error(
                    GlobalDeduplicationErrorCode.DOCUMENT_TOO_LARGE,
                    "文档超过大小限制",
                )
            chunks.append(chunk)
        return b"".join(chunks)


class DocumentReader(Protocol):
    def read(self, path: str | Path, *, max_bytes: int) -> bytes: ...


def load_documents(
    references: tuple[DocumentReference, ...],
    *,
    reader: DocumentReader,
    max_document_bytes: int,
    max_total_bytes: int,
) -> tuple[NormalizedDocument, ...]:
    documents: list[NormalizedDocument] = []
    total_bytes = 0
    for reference in references:
        storage_path = reference.file_storage_path
        content = reader.read(storage_path, max_bytes=max_document_bytes)
        total_bytes += len(content)
        if total_bytes > max_total_bytes:
            raise _input_error(
                GlobalDeduplicationErrorCode.BATCH_TOO_LARGE,
                "批次文档累计大小超过限制",
            )
        documents.append(
            NormalizedDocument(
                reference=reference,
                text=normalize_document(
                    content,
                    suffix=Path(urlsplit(storage_path).path).suffix,
                ),
                size_bytes=len(content),
            )
        )
    return tuple(documents)


class BoundedUriReader:
    def __init__(
        self,
        *,
        input_roots: tuple[Path, ...],
        chunk_bytes: int,
        remote_url_validator: Callable[[str], str] | None = None,
        http_client: httpx.Client | None = None,
        max_http_redirects: int = 3,
        allowed_s3_buckets: tuple[str, ...] = (),
        s3_storage_options: Mapping[str, object] | None = None,
    ) -> None:
        if max_http_redirects < 0:
            raise ValueError("HTTP 重定向次数不能为负数")
        self._local = BoundedLocalReader(
            input_roots=input_roots,
            chunk_bytes=chunk_bytes,
        )
        self._chunk_bytes = chunk_bytes
        self._remote_url_validator = remote_url_validator
        self._http_client = http_client or httpx.Client(follow_redirects=False)
        self._max_http_redirects = max_http_redirects
        self._allowed_s3_buckets = frozenset(allowed_s3_buckets)
        self._s3_storage_options = dict(s3_storage_options or {})

    def read(self, path: str | Path, *, max_bytes: int) -> bytes:
        return self.read_document(str(path), max_bytes=max_bytes)

    def read_document(self, uri: str, *, max_bytes: int) -> bytes:
        return self._read(uri, max_bytes=max_bytes, purpose="document")

    def read_manifest(self, uri: str, *, max_bytes: int) -> bytes:
        return self._read(uri, max_bytes=max_bytes, purpose="manifest")

    def _read(
        self,
        uri: str,
        *,
        max_bytes: int,
        purpose: Literal["manifest", "document"],
    ) -> bytes:
        parsed = urlsplit(uri)
        try:
            if parsed.scheme in {"", "file"}:
                path = (
                    self._file_uri_path(parsed.path)
                    if parsed.scheme == "file"
                    else Path(uri)
                )
                return self._local.read(path, max_bytes=max_bytes)
            if parsed.username is not None or parsed.password is not None:
                raise self._path_error(purpose)
            if parsed.scheme in {"http", "https"}:
                return self._read_http(uri, max_bytes, purpose)
            if parsed.scheme == "s3":
                return self._read_s3(parsed, max_bytes, purpose)
            raise self._path_error(purpose)
        except GlobalDeduplicationProcessingError as error:
            if purpose == "document":
                raise
            raise self._manifest_error_from(error) from None

    def _read_http(
        self,
        uri: str,
        max_bytes: int,
        purpose: Literal["manifest", "document"],
    ) -> bytes:
        if self._remote_url_validator is None:
            raise self._path_error(purpose)
        current = uri
        try:
            for redirect_count in range(self._max_http_redirects + 1):
                current = self._remote_url_validator(current)
                response = self._http_client.send(
                    self._http_client.build_request("GET", current),
                    stream=True,
                )
                if response.is_redirect:
                    location = response.headers.get("location")
                    response.close()
                    if (
                        not location
                        or redirect_count == self._max_http_redirects
                    ):
                        raise self._path_error(purpose)
                    current = urljoin(current, location)
                    continue
                response.raise_for_status()
                try:
                    return self._join_bounded(
                        response.iter_bytes(self._chunk_bytes),
                        max_bytes,
                        purpose,
                    )
                finally:
                    response.close()
        except GlobalDeduplicationProcessingError:
            raise
        except Exception:
            raise self._read_error(purpose) from None
        raise self._read_error(purpose)

    def _read_s3(
        self,
        split: SplitResult,
        max_bytes: int,
        purpose: Literal["manifest", "document"],
    ) -> bytes:
        if (
            not split.hostname
            or split.hostname not in self._allowed_s3_buckets
            or split.query
            or split.fragment
        ):
            raise self._path_error(purpose)
        try:
            filesystem = fsspec.filesystem("s3", **self._s3_storage_options)
            with filesystem.open(f"{split.hostname}{split.path}", "rb") as source:
                return self._join_bounded(
                    iter(lambda: source.read(self._chunk_bytes), b""),
                    max_bytes,
                    purpose,
                )
        except FileNotFoundError:
            raise self._not_found_error(purpose) from None
        except GlobalDeduplicationProcessingError:
            raise
        except Exception:
            raise self._read_error(purpose) from None

    @staticmethod
    def _join_bounded(
        chunks: Iterable[bytes],
        max_bytes: int,
        purpose: Literal["manifest", "document"],
    ) -> bytes:
        content: list[bytes] = []
        total = 0
        for chunk in chunks:
            total += len(chunk)
            if total > max_bytes:
                code = (
                    GlobalDeduplicationErrorCode.INPUT_MANIFEST_TOO_LARGE
                    if purpose == "manifest"
                    else GlobalDeduplicationErrorCode.DOCUMENT_TOO_LARGE
                )
                raise _input_error(code, "输入超过大小限制")
            content.append(chunk)
        return b"".join(content)

    @staticmethod
    def _file_uri_path(value: str) -> Path:
        decoded = unquote(value)
        if re.match(r"^/[A-Za-z]:/", decoded):
            decoded = decoded[1:]
        return Path(decoded)

    @staticmethod
    def _path_error(
        purpose: Literal["manifest", "document"],
    ) -> GlobalDeduplicationProcessingError:
        code = (
            GlobalDeduplicationErrorCode.INPUT_MANIFEST_ACCESS_FAILED
            if purpose == "manifest"
            else GlobalDeduplicationErrorCode.DOCUMENT_PATH_NOT_ALLOWED
        )
        return _input_error(code, "输入路径不在允许范围内")

    @staticmethod
    def _read_error(
        purpose: Literal["manifest", "document"],
    ) -> GlobalDeduplicationProcessingError:
        code = (
            GlobalDeduplicationErrorCode.INPUT_MANIFEST_ACCESS_FAILED
            if purpose == "manifest"
            else GlobalDeduplicationErrorCode.DOCUMENT_READ_FAILED
        )
        return _input_error(code, "输入读取失败")

    @staticmethod
    def _not_found_error(
        purpose: Literal["manifest", "document"],
    ) -> GlobalDeduplicationProcessingError:
        code = (
            GlobalDeduplicationErrorCode.INPUT_MANIFEST_NOT_FOUND
            if purpose == "manifest"
            else GlobalDeduplicationErrorCode.DOCUMENT_NOT_FOUND
        )
        return _input_error(code, "输入不存在")

    @staticmethod
    def _manifest_error_from(
        error: GlobalDeduplicationProcessingError,
    ) -> GlobalDeduplicationProcessingError:
        if error.code is GlobalDeduplicationErrorCode.DOCUMENT_TOO_LARGE:
            return _input_error(
                GlobalDeduplicationErrorCode.INPUT_MANIFEST_TOO_LARGE,
                "输入清单超过大小限制",
            )
        if error.code is GlobalDeduplicationErrorCode.DOCUMENT_NOT_FOUND:
            return _input_error(
                GlobalDeduplicationErrorCode.INPUT_MANIFEST_NOT_FOUND,
                "输入清单不存在",
            )
        return _input_error(
            GlobalDeduplicationErrorCode.INPUT_MANIFEST_ACCESS_FAILED,
            "输入清单访问失败",
        )
