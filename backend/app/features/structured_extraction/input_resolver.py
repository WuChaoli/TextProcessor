import hashlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from urllib.parse import urljoin, urlsplit

import fsspec  # type: ignore[import-untyped]
import httpx

from app.features.structured_extraction.errors import (
    ExtractionErrorCode,
    ExtractionProcessingError,
)
from app.features.structured_extraction.models import ExtractionTask
from app.features.structured_extraction.staging import StagingLayout


@dataclass(frozen=True)
class ResolvedInput:
    path: Path
    size_bytes: int
    sha256: str


class InputResolver:
    def __init__(
        self,
        *,
        input_roots: tuple[Path, ...],
        max_input_bytes: int,
        copy_chunk_bytes: int = 1024 * 1024,
        remote_url_validator: Callable[[str], str] | None = None,
        allowed_s3_buckets: tuple[str, ...] = (),
        s3_storage_options: Mapping[str, object] | None = None,
        http_client: httpx.Client | None = None,
        max_http_redirects: int = 3,
    ) -> None:
        if max_input_bytes <= 0 or copy_chunk_bytes <= 0 or max_http_redirects < 0:
            raise ValueError("输入大小和复制块大小必须为正数")
        self._input_roots = tuple(root.resolve(strict=False) for root in input_roots)
        self._max_input_bytes = max_input_bytes
        self._copy_chunk_bytes = copy_chunk_bytes
        self._remote_url_validator = remote_url_validator
        self._allowed_s3_buckets = frozenset(allowed_s3_buckets)
        self._s3_storage_options = dict(s3_storage_options or {})
        self._http_client = http_client or httpx.Client(follow_redirects=False)
        self._max_http_redirects = max_http_redirects

    def resolve(
        self,
        task: ExtractionTask,
        layout: StagingLayout,
    ) -> ResolvedInput:
        selected_input = (
            task.file_storage_path
            if task.selected_input_type == "local"
            else task.file_oss_url
        )
        if selected_input:
            reused = self._reuse_existing(
                task,
                layout,
                Path(urlsplit(selected_input).path).suffix,
            )
            if reused is not None:
                return reused
        if task.selected_input_type == "local":
            if not task.file_storage_path:
                raise self._access_error()
            return self._resolve_local(Path(task.file_storage_path), layout)
        if task.selected_input_type == "remote":
            if not task.file_oss_url:
                raise self._access_error()
            return self._resolve_remote(task.file_oss_url, layout)
        raise self._access_error()

    def _reuse_existing(
        self,
        task: ExtractionTask,
        layout: StagingLayout,
        suffix: str,
    ) -> ResolvedInput | None:
        if task.input_sha256 is None or task.input_size_bytes is None:
            return None
        destination = layout.source_with_suffix(suffix or ".bin")
        if not destination.is_file():
            return None
        digest = hashlib.sha256()
        total = 0
        try:
            with destination.open("rb") as source:
                while chunk := source.read(self._copy_chunk_bytes):
                    total += len(chunk)
                    digest.update(chunk)
        except OSError:
            destination.unlink(missing_ok=True)
            return None
        actual_sha256 = digest.hexdigest()
        if total != task.input_size_bytes or actual_sha256 != task.input_sha256:
            destination.unlink(missing_ok=True)
            return None
        return ResolvedInput(
            path=destination,
            size_bytes=total,
            sha256=actual_sha256,
        )

    def _resolve_local(
        self,
        source_path: Path,
        layout: StagingLayout,
    ) -> ResolvedInput:
        normalized = source_path.resolve(strict=False)
        if not any(
            normalized == root or root in normalized.parents
            for root in self._input_roots
        ):
            raise self._access_error()
        if not normalized.is_file():
            raise ExtractionProcessingError(
                ExtractionErrorCode.INPUT_NOT_FOUND,
                "输入文件不存在",
            )
        filesystem = fsspec.filesystem("file")
        try:
            with filesystem.open(str(normalized), "rb") as source:
                return self._copy(source, normalized.suffix, layout)
        except ExtractionProcessingError:
            raise
        except OSError:
            raise self._access_error() from None

    def _resolve_remote(
        self,
        source_url: str,
        layout: StagingLayout,
    ) -> ResolvedInput:
        parsed = urlsplit(source_url)
        if parsed.username is not None or parsed.password is not None:
            raise self._access_error()
        if parsed.scheme in {"http", "https"}:
            return self._resolve_http(source_url, layout)
        if parsed.scheme == "s3":
            return self._resolve_s3(source_url, layout)
        raise self._access_error()

    def _resolve_http(
        self,
        source_url: str,
        layout: StagingLayout,
    ) -> ResolvedInput:
        if self._remote_url_validator is None:
            raise self._access_error()
        current_url = source_url
        try:
            for redirect_count in range(self._max_http_redirects + 1):
                current_url = self._remote_url_validator(current_url)
                request = self._http_client.build_request("GET", current_url)
                response = self._http_client.send(request, stream=True)
                if response.is_redirect:
                    location = response.headers.get("location")
                    response.close()
                    if not location or redirect_count == self._max_http_redirects:
                        raise self._access_error()
                    current_url = urljoin(current_url, location)
                    continue
                response.raise_for_status()
                try:
                    suffix = Path(urlsplit(current_url).path).suffix
                    return self._copy_chunks(
                        response.iter_bytes(self._copy_chunk_bytes),
                        suffix,
                        layout,
                    )
                finally:
                    response.close()
            raise self._access_error()
        except ExtractionProcessingError:
            raise
        except Exception:
            raise self._access_error() from None

    def _resolve_s3(
        self,
        source_url: str,
        layout: StagingLayout,
    ) -> ResolvedInput:
        parsed = urlsplit(source_url)
        if (
            not parsed.hostname
            or parsed.hostname not in self._allowed_s3_buckets
            or parsed.query
            or parsed.fragment
        ):
            raise self._access_error()
        try:
            filesystem = fsspec.filesystem("s3", **self._s3_storage_options)
            path = f"{parsed.hostname}{parsed.path}"
            with filesystem.open(path, "rb") as source:
                return self._copy(source, Path(parsed.path).suffix, layout)
        except FileNotFoundError:
            raise ExtractionProcessingError(
                ExtractionErrorCode.INPUT_NOT_FOUND,
                "输入文件不存在",
            ) from None
        except Exception:
            raise self._access_error() from None

    def _copy(
        self,
        source: BinaryIO,
        suffix: str,
        layout: StagingLayout,
    ) -> ResolvedInput:
        def chunks() -> Iterable[bytes]:
            while chunk := source.read(self._copy_chunk_bytes):
                yield chunk

        return self._copy_chunks(chunks(), suffix, layout)

    def _copy_chunks(
        self,
        chunks: Iterable[bytes],
        suffix: str,
        layout: StagingLayout,
    ) -> ResolvedInput:
        layout.prepare()
        destination = layout.source_with_suffix(suffix or ".bin")
        part = destination.parent / f".source.{layout.task_id}.part"
        digest = hashlib.sha256()
        total = 0
        try:
            with part.open("wb") as output:
                for chunk in chunks:
                    total += len(chunk)
                    if total > self._max_input_bytes:
                        raise ExtractionProcessingError(
                            ExtractionErrorCode.INPUT_TOO_LARGE,
                            "输入文件超过大小限制",
                        )
                    digest.update(chunk)
                    output.write(chunk)
            part.replace(destination)
        except Exception:
            part.unlink(missing_ok=True)
            destination.unlink(missing_ok=True)
            raise
        return ResolvedInput(
            path=destination,
            size_bytes=total,
            sha256=digest.hexdigest(),
        )

    @staticmethod
    def _access_error() -> ExtractionProcessingError:
        return ExtractionProcessingError(
            ExtractionErrorCode.INPUT_ACCESS_FAILED,
            "输入文件访问失败",
        )
