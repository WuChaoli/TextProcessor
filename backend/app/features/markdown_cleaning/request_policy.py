import ipaddress
import socket
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import SplitResult, urlsplit, urlunsplit

from app.features.markdown_cleaning.api_errors import (
    MarkdownCleaningApiErrorCode,
    MarkdownCleaningDomainError,
)
from app.features.markdown_cleaning.schemas import MarkdownCleaningTaskCreate

AddressResolver = Callable[[str, int], Iterable[str]]


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


@dataclass(frozen=True, slots=True)
class ValidatedMarkdownCleaningRequest:
    session_id: str
    file_id: str
    file_storage_path: str | None
    file_oss_url: str | None
    selected_input_type: Literal["local", "remote"]
    target_path: str


class MarkdownCleaningRequestPolicy:
    def __init__(
        self,
        *,
        input_roots: Sequence[Path],
        output_roots: Sequence[Path],
        allowed_http_hosts: Sequence[str],
        allowed_http_cidrs: Sequence[str],
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
        self._resolver = resolver

    def validate_request(
        self,
        request: MarkdownCleaningTaskCreate,
    ) -> ValidatedMarkdownCleaningRequest:
        if request.file_storage_path is not None:
            local_path = self.validate_local_input(request.file_storage_path)
            remote_url = None
            selected_input_type: Literal["local", "remote"] = "local"
        else:
            local_path = None
            remote_url = self.validate_remote_url(request.file_oss_url or "")
            selected_input_type = "remote"
        target_path = self.validate_output_path(request.target_path)
        if local_path is not None and local_path == target_path:
            raise self._output_error("输入文件和目标路径不能相同")
        return ValidatedMarkdownCleaningRequest(
            session_id=request.session_id,
            file_id=request.file_id,
            file_storage_path=local_path,
            file_oss_url=remote_url,
            selected_input_type=selected_input_type,
            target_path=target_path,
        )

    def validate_local_input(self, raw_path: str) -> str:
        path = Path(raw_path)
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            raise self._input_path_error("输入文件不存在或不可访问") from None
        if (
            not path.is_absolute()
            or not resolved.is_file()
            or not self._is_under(resolved, self._input_roots)
        ):
            raise self._input_path_error("输入文件不在允许目录内")
        return str(resolved)

    def validate_output_path(self, raw_path: str) -> str:
        path = Path(raw_path)
        if not path.is_absolute() or not self._is_markdown(path):
            raise self._output_error("目标路径必须是允许目录内的绝对 Markdown 文件路径")
        try:
            resolved = path.resolve(strict=False)
        except (OSError, RuntimeError):
            raise self._output_error("目标路径不可用") from None
        if not self._is_under(resolved, self._output_roots):
            raise self._output_error("目标路径不在允许目录内")
        return str(resolved)

    def validate_remote_url(self, raw_url: str) -> str:
        try:
            parsed = urlsplit(raw_url)
            port = parsed.port
        except ValueError:
            raise self._url_error("输入 URL 格式无效") from None
        hostname = parsed.hostname
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or not self._is_markdown(Path(parsed.path))
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
        except (OSError, ValueError):
            raise self._url_error("输入 URL host 无法安全解析") from None
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
    def _is_markdown(path: Path) -> bool:
        return path.suffix.lower() in {".md", ".markdown"}

    @staticmethod
    def _is_under(path: Path, roots: Sequence[Path]) -> bool:
        return any(path.is_relative_to(root) for root in roots)

    @staticmethod
    def _error(
        code: MarkdownCleaningApiErrorCode,
        message: str,
    ) -> MarkdownCleaningDomainError:
        return MarkdownCleaningDomainError(code, message, http_status=400)

    @classmethod
    def _input_path_error(cls, message: str) -> MarkdownCleaningDomainError:
        return cls._error(MarkdownCleaningApiErrorCode.INPUT_PATH_NOT_ALLOWED, message)

    @classmethod
    def _output_error(cls, message: str) -> MarkdownCleaningDomainError:
        return cls._error(MarkdownCleaningApiErrorCode.OUTPUT_PATH_NOT_ALLOWED, message)

    @classmethod
    def _url_error(cls, message: str) -> MarkdownCleaningDomainError:
        return cls._error(MarkdownCleaningApiErrorCode.INPUT_URL_NOT_ALLOWED, message)
