from __future__ import annotations

import hashlib
import ipaddress
import os
import socket
import ssl
import stat
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol, cast
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit

import httpcore
import httpx

from app.features.markdown_cleaning.input_validator import (
    MarkdownInputError,
    MarkdownInputErrorCode,
)
from app.features.markdown_cleaning.staging import StagingLayout

AddressResolver = Callable[[str, int], Iterable[str]]
PinnedTransportFactory = Callable[[str, str], httpx.BaseTransport]


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


@dataclass(frozen=True, slots=True)
class ValidatedHttpTarget:
    url: str
    hostname: str
    port: int
    pinned_ip: str


class PinnedNetworkBackend(httpcore.NetworkBackend):
    """Connect to one validated IP while preserving the logical origin host."""

    def __init__(
        self,
        *,
        expected_hostname: str,
        pinned_ip: str,
        backend: httpcore.NetworkBackend | None = None,
    ) -> None:
        self._expected_hostname = expected_hostname.rstrip(".").lower()
        self._pinned_ip = pinned_ip
        self._backend = backend or httpcore.SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        if host.rstrip(".").lower() != self._expected_hostname:
            raise httpcore.ConnectError("连接目标与已验证 host 不一致")
        return self._backend.connect_tcp(
            self._pinned_ip,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        del path, timeout, socket_options
        raise httpcore.ConnectError("不允许 Unix socket")

    def sleep(self, seconds: float) -> None:
        self._backend.sleep(seconds)


class _PinnedResponseStream(httpx.SyncByteStream):
    def __init__(self, stream: Iterable[bytes], request: httpx.Request) -> None:
        self._stream = stream
        self._request = request

    def __iter__(self) -> Iterator[bytes]:
        try:
            yield from self._stream
        except (
            httpcore.TimeoutException,
            httpcore.NetworkError,
            httpcore.ProtocolError,
        ) as exc:
            raise httpx.TransportError(
                "HTTP 响应读取失败",
                request=self._request,
            ) from exc

    def close(self) -> None:
        close = getattr(self._stream, "close", None)
        if close is not None:
            close()


class PinnedTransport(httpx.BaseTransport):
    """HTTP transport whose TCP destination cannot be changed by a DNS rebind."""

    def __init__(
        self,
        *,
        expected_hostname: str,
        pinned_ip: str,
        network_backend: httpcore.NetworkBackend | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self._expected_hostname = expected_hostname.rstrip(".").lower()
        self._pool = httpcore.ConnectionPool(
            ssl_context=ssl_context or ssl.create_default_context(),
            max_connections=1,
            max_keepalive_connections=0,
            network_backend=PinnedNetworkBackend(
                expected_hostname=self._expected_hostname,
                pinned_ip=pinned_ip,
                backend=network_backend,
            ),
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        hostname = (request.url.host or "").rstrip(".").lower()
        if hostname != self._expected_hostname:
            raise httpx.ConnectError(
                "请求 host 与已验证 host 不一致",
                request=request,
            )
        try:
            response = self._pool.handle_request(
                httpcore.Request(
                    method=request.method,
                    url=httpcore.URL(
                        scheme=request.url.raw_scheme,
                        host=request.url.raw_host,
                        port=request.url.port,
                        target=request.url.raw_path,
                    ),
                    headers=request.headers.raw,
                    content=request.stream,
                    extensions=request.extensions,
                )
            )
        except (
            httpcore.TimeoutException,
            httpcore.NetworkError,
            httpcore.ProtocolError,
        ) as exc:
            raise httpx.TransportError(
                "HTTP 连接失败",
                request=request,
            ) from exc
        if not isinstance(response.stream, Iterable):
            raise httpx.ProtocolError("HTTP 响应流类型无效", request=request)
        return httpx.Response(
            status_code=response.status,
            headers=response.headers,
            stream=_PinnedResponseStream(
                cast(Iterable[bytes], response.stream),  # type: ignore[redundant-cast]
                request,
            ),
            extensions=response.extensions,
        )

    def close(self) -> None:
        self._pool.close()


def _default_pinned_transport(
    hostname: str,
    pinned_ip: str,
) -> httpx.BaseTransport:
    return PinnedTransport(expected_hostname=hostname, pinned_ip=pinned_ip)


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
        transport_factory: PinnedTransportFactory = _default_pinned_transport,
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
        self._transport_factory = transport_factory
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
        validated_http = self._validate_selected_policy(
            selected, task.selected_input_type
        )
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
            if not task.file_oss_url or validated_http is None:
                raise self._access_error()
            return self._resolve_http(validated_http, layout)
        raise self._access_error()

    def _validate_selected_policy(
        self,
        selected: str,
        selected_type: str,
    ) -> ValidatedHttpTarget | None:
        if selected_type == "local":
            self._validate_local_path(Path(selected), require_exists=False)
            return None
        if selected_type == "remote":
            return self._validate_http_url(selected)
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
        try:
            with self._open_local_source(source_path) as source:
                return self._copy(source, suffix, layout)
        except MarkdownInputError:
            raise
        except OSError:
            raise self._access_error() from None

    @contextmanager
    def _open_local_source(self, source_path: Path) -> Iterator[BinaryIO]:
        if not source_path.is_absolute():
            raise self._access_error()
        lexical = Path(os.path.abspath(source_path))
        if self._has_link_component(lexical):
            raise self._access_error()
        descriptor: int | None = None
        stream: BinaryIO | None = None
        try:
            if os.name == "nt":
                descriptor, final_path = self._open_windows_no_reparse(lexical)
            else:
                descriptor, final_path = self._open_posix_no_follow(lexical)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or not self._is_under(
                final_path, self._input_roots
            ):
                raise self._access_error()
            stream = os.fdopen(descriptor, "rb")
            descriptor = None
            yield stream
        except FileNotFoundError:
            raise self._error(
                MarkdownInputErrorCode.INPUT_NOT_FOUND,
                "输入文件不存在",
            ) from None
        except MarkdownInputError:
            raise
        except OSError:
            raise self._access_error() from None
        finally:
            if stream is not None:
                stream.close()
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _open_posix_no_follow(path: Path) -> tuple[int, Path]:
        flags = os.O_RDONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            fd_link = Path(f"/proc/self/fd/{descriptor}")
            if fd_link.exists():
                final_path = Path(os.path.realpath(fd_link))
            else:
                final_path = path.resolve(strict=True)
                path_metadata = path.stat(follow_symlinks=False)
                descriptor_metadata = os.fstat(descriptor)
                if (
                    path_metadata.st_dev != descriptor_metadata.st_dev
                    or path_metadata.st_ino != descriptor_metadata.st_ino
                ):
                    raise OSError("opened file identity changed")
            return descriptor, final_path
        except Exception:
            os.close(descriptor)
            raise

    @staticmethod
    def _open_windows_no_reparse(path: Path) -> tuple[int, Path]:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        generic_read = 0x80000000
        share_read_write_delete = 0x00000001 | 0x00000002 | 0x00000004
        open_existing = 3
        file_attribute_normal = 0x00000080
        file_attribute_directory = 0x00000010
        file_attribute_reparse_point = 0x00000400
        file_flag_open_reparse_point = 0x00200000
        file_attribute_tag_info = 9

        create_file = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        handle = create_file(
            str(path),
            generic_read,
            share_read_write_delete,
            None,
            open_existing,
            file_attribute_normal | file_flag_open_reparse_point,
            None,
        )
        invalid_handle = wintypes.HANDLE(-1).value
        if handle == invalid_handle:
            raise ctypes.WinError(ctypes.get_last_error())

        class FileAttributeTagInfo(ctypes.Structure):
            _fields_ = (
                ("FileAttributes", wintypes.DWORD),
                ("ReparseTag", wintypes.DWORD),
            )

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        info = FileAttributeTagInfo()
        try:
            get_info = kernel32.GetFileInformationByHandleEx
            get_info.argtypes = (
                wintypes.HANDLE,
                ctypes.c_int,
                wintypes.LPVOID,
                wintypes.DWORD,
            )
            get_info.restype = wintypes.BOOL
            if not get_info(
                handle,
                file_attribute_tag_info,
                ctypes.byref(info),
                ctypes.sizeof(info),
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            if info.FileAttributes & (
                file_attribute_directory | file_attribute_reparse_point
            ):
                raise OSError("local input is a directory or reparse point")
            get_final_path = kernel32.GetFinalPathNameByHandleW
            get_final_path.argtypes = (
                wintypes.HANDLE,
                wintypes.LPWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
            )
            get_final_path.restype = wintypes.DWORD
            required = get_final_path(handle, None, 0, 0)
            if required == 0:
                raise ctypes.WinError(ctypes.get_last_error())
            buffer = ctypes.create_unicode_buffer(required + 1)
            if get_final_path(handle, buffer, len(buffer), 0) == 0:
                raise ctypes.WinError(ctypes.get_last_error())
            final_path = InputResolver._normalize_windows_handle_path(buffer.value)
            descriptor = msvcrt.open_osfhandle(
                int(handle), os.O_RDONLY | getattr(os, "O_BINARY", 0)
            )
            handle = invalid_handle
            return descriptor, final_path
        finally:
            if handle != invalid_handle:
                close_handle(handle)

    @staticmethod
    def _normalize_windows_handle_path(raw_path: str) -> Path:
        if raw_path.startswith("\\\\?\\UNC\\"):
            raw_path = "\\\\" + raw_path[8:]
        elif raw_path.startswith("\\\\?\\"):
            raw_path = raw_path[4:]
        return Path(raw_path).resolve(strict=False)

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
        initial_target: ValidatedHttpTarget,
        layout: StagingLayout,
    ) -> ResolvedMarkdownInput:
        target = initial_target
        try:
            for redirect_count in range(self._max_http_redirects + 1):
                transport = self._transport_factory(target.hostname, target.pinned_ip)
                with httpx.Client(
                    transport=transport,
                    follow_redirects=False,
                    trust_env=False,
                ) as client:
                    request = client.build_request(
                        "GET", target.url, timeout=self._http_timeout
                    )
                    response = client.send(request, stream=True)
                    if response.is_redirect:
                        location = response.headers.get("location")
                        response.close()
                        if not location or redirect_count == self._max_http_redirects:
                            raise self._access_error()
                        target = self._validate_http_url(urljoin(target.url, location))
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
                        suffix = self._source_suffix(target.url, "remote")
                        return self._copy_chunks(
                            response.iter_bytes(self._copy_chunk_bytes),
                            suffix,
                            layout,
                        )
                    finally:
                        response.close()
            raise self._access_error()
        except MarkdownInputError:
            raise
        except (httpx.HTTPError, OSError, ValueError):  # fmt: skip
            raise self._access_error() from None

    def _validate_http_url(self, raw_url: str) -> ValidatedHttpTarget:
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
        except (OSError, ValueError):  # fmt: skip
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
        normalized_url = urlunsplit(
            SplitResult(
                scheme,
                hostname if port is None else f"{hostname}:{port}",
                parsed.path,
                "",
                "",
            )
        )
        return ValidatedHttpTarget(
            url=normalized_url,
            hostname=hostname,
            port=expected_port,
            pinned_ip=str(addresses[0]),
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
