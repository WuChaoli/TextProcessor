import ipaddress
import socket
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from urllib.parse import SplitResult, urlsplit, urlunsplit

from app.features.global_deduplication.api_errors import (
    GlobalDeduplicationApiErrorCode,
    GlobalDeduplicationDomainError,
)
from app.features.global_deduplication.schemas import (
    GlobalDeduplicationTaskCreate,
)

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
class ValidatedGlobalDeduplicationRequest:
    session_id: str
    input_json_path: str
    target_path: str


class GlobalDeduplicationRequestPolicy:
    def __init__(
        self,
        *,
        input_roots: Sequence[Path],
        output_roots: Sequence[Path],
        allowed_http_hosts: Sequence[str],
        allowed_http_cidrs: Sequence[str],
        allowed_s3_buckets: Sequence[str] = (),
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
        self._allowed_s3_buckets = frozenset(allowed_s3_buckets)
        self._resolver = resolver

    def validate_request(
        self,
        request: GlobalDeduplicationTaskCreate,
    ) -> ValidatedGlobalDeduplicationRequest:
        return ValidatedGlobalDeduplicationRequest(
            session_id=request.session_id,
            input_json_path=self.validate_input(request.input_json_path),
            target_path=self.validate_output(request.target_path),
        )

    def validate_input(self, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme.lower() in {"http", "https"}:
            return self._validate_http(value)
        if parsed.scheme.lower() == "s3":
            return self._validate_s3(value, output=False)
        if parsed.scheme.lower() == "file":
            value = parsed.path
        path = Path(value)
        try:
            resolved = path.resolve(strict=True)
        except OSError, RuntimeError:
            raise self._error(
                GlobalDeduplicationApiErrorCode.INPUT_PATH_NOT_ALLOWED,
                "输入清单不存在或不可访问",
            ) from None
        if (
            not path.is_absolute()
            or not resolved.is_file()
            or not any(resolved.is_relative_to(root) for root in self._input_roots)
        ):
            raise self._error(
                GlobalDeduplicationApiErrorCode.INPUT_PATH_NOT_ALLOWED,
                "输入清单不在允许目录内",
            )
        return str(resolved)

    def validate_output(self, value: str) -> str:
        parsed = urlsplit(value)
        if PureWindowsPath(value).is_absolute():
            pass
        elif parsed.scheme.lower() == "file":
            value = parsed.path
        elif parsed.scheme.lower() == "s3":
            return self._validate_s3(value, output=True)
        elif parsed.scheme:
            raise self._error(
                GlobalDeduplicationApiErrorCode.OUTPUT_PATH_NOT_ALLOWED,
                "目标路径协议未获授权",
            )
        path = Path(value)
        try:
            resolved = path.resolve(strict=False)
        except OSError, RuntimeError:
            raise self._error(
                GlobalDeduplicationApiErrorCode.OUTPUT_PATH_NOT_ALLOWED,
                "目标路径不可用",
            ) from None
        if (
            not path.is_absolute()
            or resolved.suffix.lower() != ".json"
            or not any(resolved.is_relative_to(root) for root in self._output_roots)
        ):
            raise self._error(
                GlobalDeduplicationApiErrorCode.OUTPUT_PATH_NOT_ALLOWED,
                "目标路径必须是允许目录内的绝对 JSON 文件路径",
            )
        return str(resolved)

    def _validate_s3(self, value: str, *, output: bool) -> str:
        parsed = urlsplit(value)
        bucket = parsed.hostname
        if (
            bucket is None
            or bucket not in self._allowed_s3_buckets
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not parsed.path
            or parsed.path.endswith("/")
            or (output and not parsed.path.lower().endswith(".json"))
        ):
            code = (
                GlobalDeduplicationApiErrorCode.OUTPUT_PATH_NOT_ALLOWED
                if output
                else GlobalDeduplicationApiErrorCode.INPUT_PATH_NOT_ALLOWED
            )
            raise self._error(code, "S3 路径未获授权")
        return f"s3://{bucket}{parsed.path}"

    def _validate_http(self, value: str) -> str:
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError:
            raise self._url_error("输入 URL 格式无效") from None
        hostname = parsed.hostname
        if (
            hostname is None
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
        except OSError, ValueError:
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
    def _error(
        code: GlobalDeduplicationApiErrorCode,
        message: str,
    ) -> GlobalDeduplicationDomainError:
        return GlobalDeduplicationDomainError(
            code,
            message,
            http_status=400,
        )

    @classmethod
    def _url_error(cls, message: str) -> GlobalDeduplicationDomainError:
        return cls._error(
            GlobalDeduplicationApiErrorCode.INPUT_URL_NOT_ALLOWED,
            message,
        )
