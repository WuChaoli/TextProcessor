import hashlib
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.features.markdown_cleaning.processors.errors import (
    MarkdownCleaningErrorCode,
    MarkdownCleaningProcessorError,
)


@dataclass(frozen=True)
class PreparedMarkdownResult:
    path: Path
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class PublishedMarkdownResult:
    path: Path
    sha256: str
    size_bytes: int
    recovered: bool


class MarkdownCleaningResultPublisher:
    def __init__(
        self,
        *,
        output_roots: tuple[Path, ...],
        max_output_bytes: int | None = None,
        copy_chunk_bytes: int = 1024 * 1024,
    ) -> None:
        if not output_roots:
            raise ValueError("输出根目录不能为空")
        if max_output_bytes is not None and max_output_bytes <= 0:
            raise ValueError("max_output_bytes 必须为正整数")
        if copy_chunk_bytes <= 0:
            raise ValueError("copy_chunk_bytes 必须为正整数")
        self._output_roots = tuple(root.resolve(strict=False) for root in output_roots)
        self._max_output_bytes = max_output_bytes
        self._copy_chunk_bytes = copy_chunk_bytes

    def prepare(self, source: Path) -> PreparedMarkdownResult:
        try:
            content = source.read_bytes()
            content.decode("utf-8", errors="strict")
        except OSError as exc:
            raise _invalid_output("清洗结果读取失败") from exc
        except UnicodeDecodeError as exc:
            raise _invalid_output("清洗结果包含非法 UTF-8") from exc

        if not content:
            raise _invalid_output("清洗结果为空")
        if self._max_output_bytes is not None and len(content) > self._max_output_bytes:
            raise _invalid_output("清洗结果超过允许大小")
        return PreparedMarkdownResult(
            path=source,
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
        )

    def publish(
        self,
        prepared: PreparedMarkdownResult,
        target: Path,
        *,
        allow_recovery: bool,
    ) -> PublishedMarkdownResult:
        target = self._normalize_target(target)
        if target.exists():
            return self._resolve_existing(prepared, target, allow_recovery=allow_recovery)

        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = target.parent / f".markdown-cleaning-publish-{uuid.uuid4()}.tmp"
        try:
            self._copy_to_exclusive_temporary(prepared.path, temporary)
            try:
                os.link(temporary, target)
            except OSError as exc:
                if target.exists():
                    return self._resolve_existing(
                        prepared,
                        target,
                        allow_recovery=allow_recovery,
                    )
                raise _output_conflict("清洗结果发布失败") from exc
            finally:
                self._fsync_directory(target.parent)
        except MarkdownCleaningProcessorError:
            raise
        except OSError as exc:
            if target.exists():
                return self._resolve_existing(
                    prepared,
                    target,
                    allow_recovery=allow_recovery,
                )
            raise _output_conflict("清洗结果发布失败") from exc
        finally:
            temporary.unlink(missing_ok=True)

        return PublishedMarkdownResult(
            path=target,
            sha256=prepared.sha256,
            size_bytes=prepared.size_bytes,
            recovered=False,
        )

    def _fsync_directory(self, directory: Path) -> None:
        try:
            fd = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _normalize_target(self, target: Path) -> Path:
        normalized = target.resolve(strict=False)
        if (
            not normalized.is_absolute()
            or normalized.suffix.lower() != ".md"
            or not any(
                normalized == root or normalized.is_relative_to(root)
                for root in self._output_roots
            )
        ):
            raise _output_conflict("目标路径不在允许输出目录")
        return normalized

    def _resolve_existing(
        self,
        prepared: PreparedMarkdownResult,
        target: Path,
        *,
        allow_recovery: bool,
    ) -> PublishedMarkdownResult:
        if not allow_recovery:
            raise _output_conflict("结果文件已存在")
        try:
            current = target.read_bytes()
        except OSError as exc:
            raise _output_conflict("结果文件读取失败") from exc
        if (
            len(current) == prepared.size_bytes
            and hashlib.sha256(current).hexdigest() == prepared.sha256
        ):
            return PublishedMarkdownResult(
                path=target,
                sha256=prepared.sha256,
                size_bytes=prepared.size_bytes,
                recovered=True,
            )
        raise _output_conflict("结果文件已存在")

    def _copy_to_exclusive_temporary(self, source: Path, temporary: Path) -> None:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            with (
                source.open("rb") as input_file,
                os.fdopen(descriptor, "wb") as output_file,
            ):
                descriptor = -1
                while chunk := input_file.read(self._copy_chunk_bytes):
                    output_file.write(chunk)
                output_file.flush()
                os.fsync(output_file.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)


def _invalid_output(message: str) -> MarkdownCleaningProcessorError:
    return MarkdownCleaningProcessorError(
        MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT,
        message,
    )


def _output_conflict(message: str) -> MarkdownCleaningProcessorError:
    return MarkdownCleaningProcessorError(
        MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT,
        message,
    )
