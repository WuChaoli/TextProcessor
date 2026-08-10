import errno
import hashlib
import json
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import SplitResult, urlsplit

import fsspec  # type: ignore[import-untyped]

from app.core.local_path_policy import LocalPathAccessError, LocalPathAccessPolicy
from app.features.global_deduplication.errors import (
    GlobalDeduplicationErrorCode,
    GlobalDeduplicationProcessingError,
)
from app.features.global_deduplication.result_mapper import BusinessResult


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _output_error(
    code: GlobalDeduplicationErrorCode,
    message: str,
) -> GlobalDeduplicationProcessingError:
    return GlobalDeduplicationProcessingError(code, message)


@dataclass(frozen=True, slots=True)
class PreparedFinalResult:
    path: Path
    sha256: str
    size_bytes: int
    record_count: int


@dataclass(frozen=True, slots=True)
class PublishedFinalResult:
    path: str
    sha256: str
    size_bytes: int


class FinalResultPublisher:
    def __init__(
        self,
        *,
        allowed_s3_buckets: tuple[str, ...] = (),
        s3_storage_options: Mapping[str, object] | None = None,
        local_paths: LocalPathAccessPolicy | None = None,
    ) -> None:
        self._allowed_s3_buckets = frozenset(allowed_s3_buckets)
        self._s3_storage_options = dict(s3_storage_options or {})
        self._local_paths = local_paths or LocalPathAccessPolicy()

    def exists(self, target: str | Path) -> bool:
        parsed = urlsplit(str(target))
        if parsed.scheme == "s3":
            filesystem, key = self._s3_target(parsed)
            try:
                return bool(filesystem.exists(key))
            except Exception:
                raise _output_error(
                    GlobalDeduplicationErrorCode.OUTPUT_WRITE_FAILED,
                    "无法检查目标结果路径",
                ) from None
        return Path(target).exists()

    def prepare(
        self,
        results: tuple[BusinessResult, ...],
        staging_path: Path,
    ) -> PreparedFinalResult:
        content = (
            json.dumps(
                [result.to_public_dict() for result in results],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
        ).encode()
        expected_sha256 = hashlib.sha256(content).hexdigest()
        if staging_path.exists():
            try:
                if staging_path.read_bytes() == content:
                    return PreparedFinalResult(
                        path=staging_path,
                        sha256=expected_sha256,
                        size_bytes=len(content),
                        record_count=len(results),
                    )
            except OSError:
                pass
            raise _output_error(
                GlobalDeduplicationErrorCode.OUTPUT_INTEGRITY_FAILED,
                "已有 staging 输出与当前结果不一致",
            )
        part = staging_path.with_name(f"{staging_path.name}.part")
        try:
            staging_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with part.open("xb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            parsed = json.loads(part.read_text(encoding="utf-8"))
            if not isinstance(parsed, list) or len(parsed) != len(results):
                raise _output_error(
                    GlobalDeduplicationErrorCode.OUTPUT_INTEGRITY_FAILED,
                    "staging 输出完整性校验失败",
                )
            part.replace(staging_path)
        except GlobalDeduplicationProcessingError:
            raise
        except OSError:
            raise _output_error(
                GlobalDeduplicationErrorCode.OUTPUT_WRITE_FAILED,
                "无法写入最终结果 staging",
            ) from None
        finally:
            part.unlink(missing_ok=True)
        return PreparedFinalResult(
            path=staging_path,
            sha256=expected_sha256,
            size_bytes=len(content),
            record_count=len(results),
        )

    def publish(
        self,
        prepared: PreparedFinalResult,
        target: str | Path,
        *,
        allow_recovery: bool,
    ) -> PublishedFinalResult:
        parsed = urlsplit(str(target))
        if parsed.scheme == "s3":
            return self._publish_s3(
                prepared,
                parsed,
                allow_recovery=allow_recovery,
            )
        local_target = Path(target)
        try:
            if _sha256(prepared.path) != prepared.sha256:
                raise _output_error(
                    GlobalDeduplicationErrorCode.OUTPUT_INTEGRITY_FAILED,
                    "prepared 输出摘要不一致",
                )
            local_target = self._local_paths.preflight_output(
                str(local_target), suffixes=frozenset({".json"})
            )
            try:
                os.link(prepared.path, local_target)
            except FileExistsError:
                if not allow_recovery or _sha256(local_target) != prepared.sha256:
                    raise _output_error(
                        GlobalDeduplicationErrorCode.OUTPUT_CONFLICT,
                        "目标结果文件已存在",
                    ) from None
            except OSError as error:
                if error.errno != errno.EXDEV:
                    raise
                self._publish_cross_device(
                    prepared,
                    local_target,
                    allow_recovery=allow_recovery,
                )
            return PublishedFinalResult(
                path=str(local_target),
                sha256=prepared.sha256,
                size_bytes=prepared.size_bytes,
            )
        except GlobalDeduplicationProcessingError:
            raise
        except LocalPathAccessError:
            raise _output_error(
                GlobalDeduplicationErrorCode.OUTPUT_WRITE_FAILED,
                "最终结果发布失败",
            ) from None
        except OSError:
            raise _output_error(
                GlobalDeduplicationErrorCode.OUTPUT_WRITE_FAILED,
                "最终结果发布失败",
            ) from None

    @staticmethod
    def _publish_cross_device(
        prepared: PreparedFinalResult,
        target: Path,
        *,
        allow_recovery: bool,
    ) -> None:
        part = target.with_name(f".{target.name}.{uuid.uuid4().hex}.part")
        try:
            descriptor = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with (
                os.fdopen(descriptor, "wb") as output,
                prepared.path.open("rb") as source,
            ):
                while chunk := source.read(1024 * 1024):
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if _sha256(part) != prepared.sha256:
                raise _output_error(
                    GlobalDeduplicationErrorCode.OUTPUT_INTEGRITY_FAILED,
                    "跨文件系统临时结果摘要不一致",
                )
            try:
                os.link(part, target)
            except FileExistsError:
                if not allow_recovery or _sha256(target) != prepared.sha256:
                    raise _output_error(
                        GlobalDeduplicationErrorCode.OUTPUT_CONFLICT,
                        "目标结果文件已存在",
                    ) from None
        finally:
            part.unlink(missing_ok=True)

    def _publish_s3(
        self,
        prepared: PreparedFinalResult,
        parsed: SplitResult,
        *,
        allow_recovery: bool,
    ) -> PublishedFinalResult:
        filesystem, key = self._s3_target(parsed)
        try:
            if _sha256(prepared.path) != prepared.sha256:
                raise _output_error(
                    GlobalDeduplicationErrorCode.OUTPUT_INTEGRITY_FAILED,
                    "prepared 输出摘要不一致",
                )
            if filesystem.exists(key):
                if (
                    not allow_recovery
                    or self._remote_sha256(filesystem, key) != prepared.sha256
                ):
                    raise _output_error(
                        GlobalDeduplicationErrorCode.OUTPUT_CONFLICT,
                        "目标结果文件已存在",
                    )
            else:
                with (
                    prepared.path.open("rb") as source,
                    filesystem.open(key, "xb") as output,
                ):
                    while chunk := source.read(1024 * 1024):
                        output.write(chunk)
            if self._remote_sha256(filesystem, key) != prepared.sha256:
                raise _output_error(
                    GlobalDeduplicationErrorCode.OUTPUT_INTEGRITY_FAILED,
                    "发布结果摘要不一致",
                )
            return PublishedFinalResult(
                path=f"s3://{parsed.hostname}{parsed.path}",
                sha256=prepared.sha256,
                size_bytes=prepared.size_bytes,
            )
        except GlobalDeduplicationProcessingError:
            raise
        except FileExistsError:
            raise _output_error(
                GlobalDeduplicationErrorCode.OUTPUT_CONFLICT,
                "目标结果文件已存在",
            ) from None
        except Exception:
            raise _output_error(
                GlobalDeduplicationErrorCode.OUTPUT_WRITE_FAILED,
                "最终结果发布失败",
            ) from None

    def _s3_target(self, parsed: SplitResult) -> tuple[Any, str]:
        bucket = parsed.hostname
        if (
            bucket is None
            or bucket not in self._allowed_s3_buckets
            or parsed.query
            or parsed.fragment
            or not parsed.path
            or parsed.path.endswith("/")
        ):
            raise _output_error(
                GlobalDeduplicationErrorCode.OUTPUT_PATH_NOT_ALLOWED,
                "S3 目标路径不在允许范围内",
            )
        filesystem = fsspec.filesystem("s3", **self._s3_storage_options)
        return filesystem, f"{bucket}{parsed.path}"

    @staticmethod
    def _remote_sha256(filesystem: Any, key: str) -> str:
        digest = hashlib.sha256()
        with filesystem.open(key, "rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()
