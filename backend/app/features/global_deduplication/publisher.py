import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

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
    path: Path
    sha256: str
    size_bytes: int


class FinalResultPublisher:
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
        target: Path,
        *,
        allow_recovery: bool,
    ) -> PublishedFinalResult:
        try:
            if _sha256(prepared.path) != prepared.sha256:
                raise _output_error(
                    GlobalDeduplicationErrorCode.OUTPUT_INTEGRITY_FAILED,
                    "prepared 输出摘要不一致",
                )
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                os.link(prepared.path, target)
            except FileExistsError:
                if not allow_recovery or _sha256(target) != prepared.sha256:
                    raise _output_error(
                        GlobalDeduplicationErrorCode.OUTPUT_CONFLICT,
                        "目标结果文件已存在",
                    ) from None
            return PublishedFinalResult(
                path=target,
                sha256=prepared.sha256,
                size_bytes=prepared.size_bytes,
            )
        except GlobalDeduplicationProcessingError:
            raise
        except OSError:
            raise _output_error(
                GlobalDeduplicationErrorCode.OUTPUT_WRITE_FAILED,
                "最终结果发布失败",
            ) from None
