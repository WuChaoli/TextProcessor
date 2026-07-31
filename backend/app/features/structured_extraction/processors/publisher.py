import hashlib
import os
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.features.structured_extraction.errors import (
    ExtractionErrorCode,
    ExtractionProcessingError,
)


@dataclass(frozen=True)
class PreparedOutput:
    path: Path
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class PublishedOutput:
    path: Path
    sha256: str
    size_bytes: int
    recovered: bool


class AtomicPublisher:
    def __init__(
        self,
        *,
        max_output_bytes: int,
        output_roots: tuple[Path, ...],
        copy_chunk_bytes: int = 1024 * 1024,
    ) -> None:
        if max_output_bytes <= 0 or copy_chunk_bytes <= 0 or not output_roots:
            raise ValueError("发布大小、复制块和输出根目录配置无效")
        self._max_output_bytes = max_output_bytes
        self._copy_chunk_bytes = copy_chunk_bytes
        self._output_roots = tuple(root.resolve(strict=False) for root in output_roots)
        self._publish_lock = threading.Lock()

    def prepare(self, source: Path) -> PreparedOutput:
        try:
            content = source.read_bytes()
            content.decode("utf-8", errors="strict")
        except OSError, UnicodeDecodeError:
            raise invalid_output() from None
        if (
            not content
            or content.startswith(b"\xef\xbb\xbf")
            or len(content) > self._max_output_bytes
        ):
            raise invalid_output()
        return PreparedOutput(
            path=source,
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
        )

    def ensure_target_available(self, target: Path) -> Path:
        normalized_target = target.resolve(strict=False)
        self._validate_target(normalized_target)
        if normalized_target.exists():
            raise output_conflict()
        return normalized_target

    def publish(
        self,
        prepared: PreparedOutput,
        target: Path,
        *,
        allow_recovery: bool = False,
    ) -> PublishedOutput:
        with self._publish_lock:
            return self._publish_locked(
                prepared,
                target,
                allow_recovery=allow_recovery,
            )

    def _publish_locked(
        self,
        prepared: PreparedOutput,
        target: Path,
        *,
        allow_recovery: bool,
    ) -> PublishedOutput:
        normalized_target = target.resolve(strict=False)
        self._validate_target(normalized_target)
        if normalized_target.exists():
            return self._resolve_existing(
                prepared,
                normalized_target,
                allow_recovery=allow_recovery,
            )
        normalized_target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = normalized_target.parent / f".publish-{uuid.uuid4()}.tmp"
        try:
            self._copy_to_exclusive_temporary(prepared.path, temporary)
            try:
                os.link(temporary, normalized_target)
            except FileExistsError:
                return self._resolve_existing(
                    prepared,
                    normalized_target,
                    allow_recovery=allow_recovery,
                )
        except ExtractionProcessingError:
            raise
        except OSError:
            if normalized_target.exists():
                return self._resolve_existing(
                    prepared,
                    normalized_target,
                    allow_recovery=allow_recovery,
                )
            raise ExtractionProcessingError(
                ExtractionErrorCode.OUTPUT_WRITE_FAILED,
                "结构化提取结果发布失败",
                transient=True,
            ) from None
        finally:
            temporary.unlink(missing_ok=True)
        return PublishedOutput(
            path=normalized_target,
            sha256=prepared.sha256,
            size_bytes=prepared.size_bytes,
            recovered=False,
        )

    def _validate_target(self, target: Path) -> None:
        if (
            not target.is_absolute()
            or target.suffix.lower() != ".md"
            or not any(
                target == root or root in target.parents for root in self._output_roots
            )
        ):
            raise ExtractionProcessingError(
                ExtractionErrorCode.OUTPUT_WRITE_FAILED,
                "目标文件不在允许的输出目录",
            )

    def _copy_to_exclusive_temporary(self, source: Path, temporary: Path) -> None:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            with (
                source.open("rb") as input_file,
                os.fdopen(
                    descriptor,
                    "wb",
                ) as output_file,
            ):
                descriptor = -1
                while chunk := input_file.read(self._copy_chunk_bytes):
                    output_file.write(chunk)
                output_file.flush()
                os.fsync(output_file.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _resolve_existing(
        self,
        prepared: PreparedOutput,
        target: Path,
        *,
        allow_recovery: bool,
    ) -> PublishedOutput:
        if allow_recovery:
            try:
                content = target.read_bytes()
            except OSError:
                raise output_conflict() from None
            if (
                len(content) == prepared.size_bytes
                and hashlib.sha256(content).hexdigest() == prepared.sha256
            ):
                return PublishedOutput(
                    path=target,
                    sha256=prepared.sha256,
                    size_bytes=prepared.size_bytes,
                    recovered=True,
                )
        raise output_conflict()


def invalid_output() -> ExtractionProcessingError:
    return ExtractionProcessingError(
        ExtractionErrorCode.INVALID_PROCESSOR_OUTPUT,
        "结构化提取结果为空、过大或编码无效",
    )


def output_conflict() -> ExtractionProcessingError:
    return ExtractionProcessingError(
        ExtractionErrorCode.OUTPUT_CONFLICT,
        "目标文件已存在",
    )
