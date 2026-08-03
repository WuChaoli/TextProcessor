from __future__ import annotations

import hashlib
import ipaddress
import os
import socket
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit

import fsspec  # type: ignore[import-untyped]
import httpx

from app.features.markdown_cleaning.input_validator import (
    MarkdownInputError,
    MarkdownInputErrorCode,
)
from app.features.markdown_cleaning.staging import StagingLayout

AddressResolver = Callable[[str, int], Iterable[str]]


def _system_resolver(host: str, port: int) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                str(address[4][0])
                for address in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            }
        )
    )


class MarkdownInputTask(Protocol):
    file_storage_path: str | None
    file_oss_url: str | None
    selected_input_type: str
    input_sha256: str | None


@dataclass(frozen=True, slots=True)
class ResolvedMarkdownInput:
    path: Path
    size_bytes: int
    sha256: str
    source_suffix: str


class InputResolver:
    def __init__(
        self,
        *,
        input_roots: Sequence[Path],
        allowed_http_hosts: Sequence[str],
        allowed_http_cidrs: Sequence[str],
        max_input_bytes: int,
        copy_chunk_bytes: int = 1024 * 1024,
        connect_timeout_seconds: float = 10,
        read_timeout_seconds: float = 60,
        max_http_redirects: int = 3,
        address_resolver: AddressResolver = _system_resolver,
        http_client: httpx.Client | None = None,
    ) -> None:
        if max_input_bytes <= 0 or copy_chunk_bytes <= 0:
            raise ValueError("输入限制必须是正整数")
        if connect_timeout_seconds <= 0 or read_timeout_seconds <= 0:
            raise ValueError("HTTP 超时必须大于零")
        if max_http_redirects < 0:
            raise ValueError("HTTP 重定向次数不能为负数")
        self._input_roots = tuple(
            Path(os.path.abspath(root)).resolve(strict=False) for root in input_roots
        )
        self._allowed_http_hosts = frozenset(
            host.strip().rstrip(".").lower()
            for host in allowed_http_hosts
            if host.strip()
        )
        self._allowed_http_networks = tuple(
            ipaddress.ip_network(cidr, strict=False) for cidr in allowed_http_cidrs
        )
        self._max_input_bytes = max_input_bytes
        self._copy_chunk_bytes = copy_chunk_bytes
        self._max_http_redirects = max_http_redirects
        self._address_resolver = address_resolver
        self._http_client = http_client or httpx.Client(follow_redirects=False)
        self._http_timeout = httpx.Timeout(
            read_timeout_seconds,
            connect=connect_timeout_seconds,
            write=read_timeout_seconds,
            pool=connect_timeout_seconds,
        )

    def resolve(
        self,
        task: MarkdownInputTask,
        layout: StagingLayout,
    ) -> ResolvedMarkdownInput:
        selected = self._selected_value(task)
        suffix = self._source_suffix(selected, task.selected_input_type)
        self._validate_selected_policy(selected, task.selected_input_type)
        reused = self._reuse_existing(task, layout, suffix)
        if reused is not None:
            return reused
        if task.selected_input_type == "local":
            if not task.file_storage_path:
                raise self._error(
                    MarkdownInputErrorCode.INPUT_NOT_FOUND,
                    "输入文件不存在",
                )
            return self._resolve_local(Path(task.file_storage_path), suffix, layout)
        if task.selected_input_type == "remote":
            if not task.file_oss_url:
                raise self._access_error()
            return self._resolve_http(task.file_oss_url, layout)
        raise self._access_error()

    def _validate_selected_policy(self, selected: str, selected_type: str) -> None:
        if selected_type == "local":
            self._validate_local_path(Path(selected), require_exists=False)
            return
        if selected_type == "remote":
            self._validate_http_url(selected)
            return
        raise self._access_error()

    @staticmethod
    def _selected_value(task: MarkdownInputTask) -> str:
        if task.selected_input_type == "local":
            if not task.file_storage_path:
                raise InputResolver._error(
                    MarkdownInputErrorCode.INPUT_NOT_FOUND,
                    "输入文件不存在",
                )
            return task.file_storage_path
        if task.selected_input_type == "remote":
            if not task.file_oss_url:
                raise InputResolver._access_error()
            return task.file_oss_url
        raise InputResolver._access_error()

    def _source_suffix(self, value: str, selected_type: str) -> str:
        try:
            path = (
                Path(value) if selected_type == "local" else Path(urlsplit(value).path)
            )
        except ValueError:
            raise self._access_error() from None
        suffix = path.suffix.lower()
        if suffix not in {".md", ".markdown"}:
            raise self._error(
                MarkdownInputErrorCode.UNSUPPORTED_INPUT_FORMAT,
                "输入文件格式不受支持",
            )
        return suffix

    def _reuse_existing(
        self,
        task: MarkdownInputTask,
        layout: StagingLayout,
        suffix: str,
    ) -> ResolvedMarkdownInput | None:
        expected_sha256 = task.input_sha256
        expected_size = getattr(task, "input_size_bytes", None)
        if expected_sha256 is None:
            return None
        layout.prepare()
        destination = layout.original_source
        if destination.is_symlink() or not destination.is_file():
            return None
        digest = hashlib.sha256()
        total = 0
        try:
            with destination.open("rb") as source:
                while chunk := source.read(self._copy_chunk_bytes):
                    total += len(chunk)
                    if total > self._max_input_bytes:
                        destination.unlink(missing_ok=True)
                        return None
                    digest.update(chunk)
        except OSError:
            destination.unlink(missing_ok=True)
            return None
        actual_sha256 = digest.hexdigest()
        if actual_sha256 != expected_sha256 or (
            expected_size is not None and total != expected_size
        ):
            destination.unlink(missing_ok=True)
            return None
        return ResolvedMarkdownInput(
            path=destination,
            size_bytes=total,
            sha256=actual_sha256,
            source_suffix=suffix,
        )

    def _resolve_local(
        self,
        source_path: Path,
        suffix: str,
        layout: StagingLayout,
    ) -> ResolvedMarkdownInput:
        normalized = self._validate_local_path(source_path, require_exists=True)
        filesystem = fsspec.filesystem("file")
        try:
            with filesystem.open(str(normalized), "rb") as source:
                return self._copy(source, suffix, layout)
        except MarkdownInputError:
            raise
        except OSError:
            raise self._access_error() from None

    def _validate_local_path(
        self,
        source_path: Path,
        *,
        require_exists: bool,
    ) -> Path:
        if not source_path.is_absolute():
            raise self._access_error()
        lexical = Path(os.path.abspath(source_path))
        try:
            normalized = lexical.resolve(strict=require_exists)
        except FileNotFoundError:
            raise self._error(
                MarkdownInputErrorCode.INPUT_NOT_FOUND,
                "输入文件不存在",
            ) from None
        except OSError:
            raise self._access_error() from None
        if (
            not self._is_under(normalized, self._input_roots)
            or self._has_link_component(lexical)
            or (require_exists and not normalized.is_file())
        ):
            raise self._access_error()
        return normalized

    def _resolve_http(
        self,
        source_url: str,
        layout: StagingLayout,
    ) -> ResolvedMarkdownInput:
        current_url = source_url
        try:
            for redirect_count in range(self._max_http_redirects + 1):
                current_url = self._validate_http_url(current_url)
                request = self._http_client.build_request(
                    "GET", current_url, timeout=self._http_timeout
                )
                response = self._http_client.send(request, stream=True)
                if response.is_redirect:
                    location = response.headers.get("location")
                    response.close()
                    if not location or redirect_count == self._max_http_redirects:
                        raise self._access_error()
                    current_url = urljoin(current_url, location)
                    continue
                response.raise_for_status()
                content_length = response.headers.get("content-length")
                if content_length is not None:
                    try:
                        if int(content_length) > self._max_input_bytes:
                            raise self._error(
                                MarkdownInputErrorCode.INPUT_TOO_LARGE,
                                "输入文件超过大小限制",
                            )
                    except ValueError:
                        raise self._access_error() from None
                try:
                    suffix = self._source_suffix(current_url, "remote")
                    return self._copy_chunks(
                        response.iter_bytes(self._copy_chunk_bytes), suffix, layout
                    )
                finally:
                    response.close()
            raise self._access_error()
        except MarkdownInputError:
            raise
        except httpx.HTTPError, OSError, ValueError:
            raise self._access_error() from None

    def _validate_http_url(self, raw_url: str) -> str:
        try:
            parsed = urlsplit(raw_url)
            port = parsed.port
        except ValueError:
            raise self._access_error() from None
        hostname = parsed.hostname
        scheme = parsed.scheme.lower()
        if (
            scheme not in {"http", "https"}
            or hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise self._access_error()
        hostname = hostname.rstrip(".").lower()
        if hostname not in self._allowed_http_hosts:
            raise self._access_error()
        expected_port = 443 if scheme == "https" else 80
        if port is not None and port != expected_port:
            raise self._access_error()
        self._source_suffix(raw_url, "remote")
        try:
            addresses = tuple(
                ipaddress.ip_address(value)
                for value in self._address_resolver(hostname, expected_port)
            )
        except OSError, ValueError:
            raise self._access_error() from None
        if not addresses or any(
            address.is_loopback
            or address.is_link_local
            or address.is_unspecified
            or address.is_multicast
            or address.is_reserved
            or not any(address in network for network in self._allowed_http_networks)
            for address in addresses
        ):
            raise self._access_error()
        return urlunsplit(
            SplitResult(
                scheme,
                hostname if port is None else f"{hostname}:{port}",
                parsed.path,
                "",
                "",
            )
        )

    def _copy(
        self,
        source: BinaryIO,
        suffix: str,
        layout: StagingLayout,
    ) -> ResolvedMarkdownInput:
        def chunks() -> Iterable[bytes]:
            while chunk := source.read(self._copy_chunk_bytes):
                yield chunk

        return self._copy_chunks(chunks(), suffix, layout)

    def _copy_chunks(
        self,
        chunks: Iterable[bytes],
        suffix: str,
        layout: StagingLayout,
    ) -> ResolvedMarkdownInput:
        layout.prepare()
        destination = layout.original_source
        part = layout.input_dir / ".source.original.md.part"
        digest = hashlib.sha256()
        total = 0
        try:
            part.unlink(missing_ok=True)
            with part.open("xb") as output:
                for chunk in chunks:
                    total += len(chunk)
                    if total > self._max_input_bytes:
                        raise self._error(
                            MarkdownInputErrorCode.INPUT_TOO_LARGE,
                            "输入文件超过大小限制",
                        )
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            os.replace(part, destination)
        except Exception:
            part.unlink(missing_ok=True)
            destination.unlink(missing_ok=True)
            raise
        return ResolvedMarkdownInput(
            path=destination,
            size_bytes=total,
            sha256=digest.hexdigest(),
            source_suffix=suffix,
        )

    @staticmethod
    def _has_link_component(path: Path) -> bool:
        current = Path(path.anchor)
        for part in path.parts[1:]:
            current /= part
            if current.is_symlink() or (
                hasattr(os.path, "isjunction") and os.path.isjunction(current)
            ):
                return True
        return False

    @staticmethod
    def _is_under(path: Path, roots: Sequence[Path]) -> bool:
        return any(path.is_relative_to(root) for root in roots)

    @staticmethod
    def _access_error() -> MarkdownInputError:
        return MarkdownInputError(
            MarkdownInputErrorCode.INPUT_ACCESS_FAILED,
            "输入文件访问失败",
        )

    @staticmethod
    def _error(
        code: MarkdownInputErrorCode,
        message: str,
    ) -> MarkdownInputError:
        return MarkdownInputError(code, message)
