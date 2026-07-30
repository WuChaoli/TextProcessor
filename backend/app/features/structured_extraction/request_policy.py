import ipaddress
import socket
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import SplitResult, urlsplit, urlunsplit

from app.core.config import Settings
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


def _is_under(path: Path, roots: Sequence[Path]) -> bool:
    return any(path.is_relative_to(root) for root in roots)


class RequestPolicy:
    def __init__(
        self,
        *,
        input_roots: Sequence[Path],
        output_roots: Sequence[Path],
        allowed_http_hosts: Sequence[str],
        allowed_http_cidrs: Sequence[str],
        max_input_bytes: int,
        resolver: AddressResolver = _system_resolver,
    ) -> None:
        self._input_roots = tuple(root.resolve(strict=False) for root in input_roots)
        self._output_roots = tuple(root.resolve(strict=False) for root in output_roots)
        self._allowed_http_hosts = frozenset(
            host.rstrip(".").lower() for host in allowed_http_hosts
        )
        self._allowed_http_networks = tuple(
            ipaddress.ip_network(cidr) for cidr in allowed_http_cidrs
        )
        self._max_input_bytes = max_input_bytes
        self._resolver = resolver

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
        path = Path(raw_path)
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise self._path_error(
                ExtractionErrorCode.INPUT_PATH_NOT_ALLOWED,
                "输入文件不存在或不可访问",
            ) from exc
        if (
            not path.is_absolute()
            or not resolved.is_file()
            or not _is_under(resolved, self._input_roots)
        ):
            raise self._path_error(
                ExtractionErrorCode.INPUT_PATH_NOT_ALLOWED,
                "输入文件不在允许目录内",
            )
        try:
            too_large = resolved.stat().st_size > self._max_input_bytes
        except OSError as exc:
            raise self._path_error(
                ExtractionErrorCode.INPUT_PATH_NOT_ALLOWED,
                "无法读取输入文件信息",
            ) from exc
        if too_large:
            raise self._path_error(
                ExtractionErrorCode.INPUT_PATH_NOT_ALLOWED,
                "输入文件超过大小限制",
            )
        return str(resolved)

    def validate_output_path(self, raw_path: str) -> str:
        path = Path(raw_path)
        if not path.is_absolute() or path.suffix.lower() != ".md":
            raise self._path_error(
                ExtractionErrorCode.OUTPUT_PATH_NOT_ALLOWED,
                "目标路径必须是允许目录内的绝对 Markdown 文件路径",
            )
        try:
            resolved = path.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise self._path_error(
                ExtractionErrorCode.OUTPUT_PATH_NOT_ALLOWED,
                "目标路径不可用",
            ) from exc
        if not _is_under(resolved, self._output_roots):
            raise self._path_error(
                ExtractionErrorCode.OUTPUT_PATH_NOT_ALLOWED,
                "目标路径不在允许目录内",
            )
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
        input_roots=settings.EXTRACTION_INPUT_ROOTS,
        output_roots=settings.EXTRACTION_OUTPUT_ROOTS,
        allowed_http_hosts=settings.EXTRACTION_HTTP_ALLOWED_HOSTS,
        allowed_http_cidrs=settings.EXTRACTION_HTTP_ALLOWED_CIDRS,
        max_input_bytes=settings.EXTRACTION_MAX_INPUT_BYTES,
    ).validate_request(request)
