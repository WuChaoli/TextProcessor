import os
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from urllib.parse import unquote, urlsplit

from app.core.local_path_policy import LocalPathAccessPolicy
from app.features.global_deduplication.api_errors import (
    GlobalDeduplicationApiErrorCode,
    GlobalDeduplicationDomainError,
)
from app.features.global_deduplication.schemas import (
    GlobalDeduplicationTaskCreate,
)


@dataclass(frozen=True, slots=True)
class ValidatedGlobalDeduplicationRequest:
    session_id: str
    input_path: str


class GlobalDeduplicationRequestPolicy:
    def __init__(
        self,
        *,
        input_roots: Sequence[Path] = (),
        output_roots: Sequence[Path] = (),
        allowed_http_hosts: Sequence[str] = (),
        allowed_http_cidrs: Sequence[str] = (),
        allowed_s3_buckets: Sequence[str] = (),
        local_paths: LocalPathAccessPolicy | None = None,
    ) -> None:
        del input_roots, output_roots
        self._local_paths = local_paths or LocalPathAccessPolicy()
        del allowed_http_hosts, allowed_http_cidrs, allowed_s3_buckets

    def validate_request(
        self,
        request: GlobalDeduplicationTaskCreate,
    ) -> ValidatedGlobalDeduplicationRequest:
        return ValidatedGlobalDeduplicationRequest(
            session_id=request.session_id,
            input_path=self.validate_input(request.input_path),
        )

    def validate_input(self, value: str) -> str:
        parsed = urlsplit(value)
        if PureWindowsPath(value).is_absolute():
            pass
        elif parsed.scheme.lower() == "file":
            value = unquote(parsed.path)
            if len(value) >= 3 and value[0] == "/" and value[2] == ":":
                value = value[1:]
        elif parsed.scheme:
            raise self._error(
                GlobalDeduplicationApiErrorCode.INPUT_PATH_NOT_ALLOWED,
                "输入目录协议未获授权",
            )
        try:
            resolved = Path(value).resolve(strict=True)
            if not resolved.is_dir() or resolved.is_symlink():
                raise OSError
            original = resolved / "original"
            duplicate = resolved / "duplicate"
            for child in (original, duplicate):
                details = child.lstat()
                if child.is_symlink() or not stat.S_ISDIR(details.st_mode):
                    raise OSError
            if os.path.samefile(original, duplicate):
                raise OSError
        except OSError, RuntimeError:
            raise self._error(
                GlobalDeduplicationApiErrorCode.INPUT_ACCESS_FAILED,
                "输入目录不存在或不符合约定",
            ) from None
        return str(resolved)

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
