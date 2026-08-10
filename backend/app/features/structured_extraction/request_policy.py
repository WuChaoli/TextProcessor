import ipaddress
import os
import socket
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Literal
from urllib.parse import SplitResult, urlsplit, urlunsplit

from app.core.config import Settings
from app.core.local_path_policy import LocalPathAccessError, LocalPathAccessPolicy
from app.features.structured_extraction.errors import (
    ExtractionDomainError,
    ExtractionErrorCode,
)
from app.features.structured_extraction.schemas import ExtractionTaskCreate

AddressResolver = Callable[[str, int], Iterable[str]]


@dataclass(frozen=True)
class ValidatedExtractionRequest:
    session_id: str
    file_id: str
    file_storage_path: str | None
    file_oss_url: str | None
    selected_input_type: Literal["local", "remote"]
    target_path: str


def _system_resolver(host: str, port: int) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                str(address[4][0])
                for address in socket.getaddrinfo(
                    host,
                    port,
                    type=socket.SOCK_STREAM,
                )
            }
        )
    )


class RequestPolicy:
    def __init__(
        self,
        *,
        allowed_http_hosts: Sequence[str],
        allowed_http_cidrs: Sequence[str],
        max_input_bytes: int,
        resolver: AddressResolver = _system_resolver,
        local_paths: LocalPathAccessPolicy | None = None,
    ) -> None:
        self._allowed_http_hosts = frozenset(
            host.rstrip(".").lower() for host in allowed_http_hosts
        )
        self._allowed_http_networks = tuple(
            ipaddress.ip_network(cidr) for cidr in allowed_http_cidrs
        )
        self._max_input_bytes = max_input_bytes
        self._resolver = resolver
        self._local_paths = local_paths or LocalPathAccessPolicy()

    def validate_request(
        self,
        request: ExtractionTaskCreate,
    ) -> ValidatedExtractionRequest:
        local_path = (
            self.validate_local_input(request.file_storage_path)
            if request.file_storage_path
            else None
        )
        remote_url = (
            self.validate_remote_url(request.file_oss_url)
            if request.file_oss_url
            else None
        )
        return ValidatedExtractionRequest(
            session_id=request.session_id,
            file_id=request.file_id,
            file_storage_path=local_path,
            file_oss_url=remote_url,
            selected_input_type="local" if local_path else "remote",
            target_path=self.validate_output_path(request.target_path),
        )

    def validate_local_input(self, raw_path: str) -> str:
        try:
            resolved = self._local_paths.preflight_input(raw_path)
            with self._local_paths.open_regular_input(resolved) as source:
                size_bytes = os.fstat(source.fileno()).st_size
        except LocalPathAccessError:
            raise self._path_error(
                ExtractionErrorCode.INPUT_ACCESS_FAILED,
                "输入文件访问失败",
            ) from None
        if size_bytes > self._max_input_bytes:
            raise self._path_error(
                ExtractionErrorCode.INPUT_TOO_LARGE,
                "输入文件超过大小限制",
            )
        return str(resolved)

    def validate_output_path(self, raw_path: str) -> str:
        try:
            resolved = self._local_paths.preflight_output(
                raw_path,
                suffixes=frozenset({".md"}),
            )
        except LocalPathAccessError as error:
            code = (
                ExtractionErrorCode.INVALID_REQUEST
                if error.reason in {"not_absolute", "unsupported_suffix"}
                else ExtractionErrorCode.OUTPUT_ACCESS_FAILED
            )
            raise self._path_error(
                code,
                "目标路径不可用",
            ) from None
        return str(resolved)

    def validate_remote_url(self, raw_url: str) -> str:
        try:
            parsed = urlsplit(raw_url)
            port = parsed.port
        except ValueError as exc:
            raise self._url_error("输入 URL 格式无效") from exc
        hostname = parsed.hostname
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise self._url_error("输入 URL 不符合安全策略")
        hostname = hostname.rstrip(".").lower()
        if hostname not in self._allowed_http_hosts:
            raise self._url_error("输入 URL host 未获授权")
        expected_port = 443 if parsed.scheme.lower() == "https" else 80
        if port is not None and port != expected_port:
            raise self._url_error("输入 URL port 未获授权")
        try:
            addresses = tuple(
                ipaddress.ip_address(value)
                for value in self._resolver(hostname, expected_port)
            )
        except (OSError, ValueError) as exc:
            raise self._url_error("输入 URL host 无法安全解析") from exc
        if not addresses or any(
            address.is_loopback
            or address.is_link_local
            or address.is_unspecified
            or address.is_multicast
            or address.is_reserved
            or not any(address in network for network in self._allowed_http_networks)
            for address in addresses
        ):
            raise self._url_error("输入 URL 地址未获授权")
        normalized = SplitResult(
            parsed.scheme.lower(),
            hostname if port is None else f"{hostname}:{port}",
            parsed.path,
            parsed.query,
            "",
        )
        return urlunsplit(normalized)

    @staticmethod
    def _path_error(
        code: ExtractionErrorCode,
        message: str,
    ) -> ExtractionDomainError:
        return ExtractionDomainError(code, message, http_status=400)

    @staticmethod
    def _url_error(message: str) -> ExtractionDomainError:
        return ExtractionDomainError(
            ExtractionErrorCode.INPUT_URL_NOT_ALLOWED,
            message,
            http_status=400,
        )


def validate_request_policy(
    request: ExtractionTaskCreate,
    settings: Settings,
) -> ValidatedExtractionRequest:
    return RequestPolicy(
        allowed_http_hosts=settings.EXTRACTION_HTTP_ALLOWED_HOSTS,
        allowed_http_cidrs=settings.EXTRACTION_HTTP_ALLOWED_CIDRS,
        max_input_bytes=settings.EXTRACTION_MAX_INPUT_BYTES,
    ).validate_request(request)
