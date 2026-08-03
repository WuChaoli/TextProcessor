import errno
import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from app.features.markdown_cleaning.processors.errors import (
    MarkdownCleaningErrorCode,
    MarkdownCleaningProcessorError,
)
from app.features.markdown_cleaning.processors.markdown_parser import (
    MarkdownBlockType,
    MarkdownParserAdapter,
)
from app.features.markdown_cleaning.processors.models import ProcessorResult

_BOM = b"\xef\xbb\xbf"
_INTERNAL_TOKEN = re.compile(r"(?:__MD_INTERNAL_\d+__|\ue000MD_CLEAN_\d+\ue001)")


@dataclass(frozen=True, slots=True)
class _ProtectedUnit:
    kind: str
    parent: str | None
    content: str


def validate_pipeline_output(
    result: ProcessorResult,
    *,
    expected_input_sha256: str,
    max_output_bytes: int,
    expected_output_path: Path | None = None,
    source_path: Path | None = None,
    protected_baseline: str | None = None,
) -> ProcessorResult:
    _validate_contract_version(result)
    _validate_input(result, expected_input_sha256)
    _validate_output_blob(
        result,
        max_output_bytes=max_output_bytes,
        expected_output_path=expected_output_path,
        source_path=source_path,
        protected_baseline=protected_baseline,
    )
    _validate_summary(result)
    return result


class MarkdownCleaningOutputValidator:
    def validate(
        self,
        result: ProcessorResult,
        *,
        expected_input_sha256: str,
        max_output_bytes: int,
        expected_output_path: Path | None = None,
        source_path: Path | None = None,
        protected_baseline: str | None = None,
    ) -> ProcessorResult:
        return validate_pipeline_output(
            result,
            expected_input_sha256=expected_input_sha256,
            max_output_bytes=max_output_bytes,
            expected_output_path=expected_output_path,
            source_path=source_path,
            protected_baseline=protected_baseline,
        )


def _validate_contract_version(result: ProcessorResult) -> None:
    if result.contract_version != "markdown_cleaning_v1":
        raise MarkdownCleaningProcessorError(
            MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT,
            "处理结果协议版本不匹配",
        )


def _validate_input(result: ProcessorResult, expected_input_sha256: str) -> None:
    if not expected_input_sha256:
        raise MarkdownCleaningProcessorError(
            MarkdownCleaningErrorCode.INVALID_MARKDOWN_INPUT,
            "缺少输入摘要",
        )
    if result.input_sha256 != expected_input_sha256:
        raise MarkdownCleaningProcessorError(
            MarkdownCleaningErrorCode.INVALID_MARKDOWN_INPUT,
            "输入摘要与处理结果不一致",
        )


def _validate_output_blob(
    result: ProcessorResult,
    max_output_bytes: int,
    *,
    expected_output_path: Path | None = None,
    source_path: Path | None = None,
    protected_baseline: str | None = None,
) -> None:
    if result.output_bytes <= 0:
        raise MarkdownCleaningProcessorError(
            MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT,
            "清洗结果为空",
        )
    if max_output_bytes > 0 and result.output_bytes > max_output_bytes:
        raise MarkdownCleaningProcessorError(
            MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT,
            "清洗结果超出限制",
        )

    if protected_baseline is not None:
        _validate_sha256_hex("受保护基线", protected_baseline)
        if result.output_sha256 == protected_baseline:
            raise MarkdownCleaningProcessorError(
                MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT,
                "清洗结果摘要与受保护基线重复",
            )

    source_text = None
    if source_path is not None:
        source_text = _read_source_path(source_path, result.input_sha256)

    path = result.output_path
    if not path.is_absolute():
        raise MarkdownCleaningProcessorError(
            MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT,
            "清洗结果路径不安全",
        )
    if path.suffix.lower() != ".md":
        raise MarkdownCleaningProcessorError(
            MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT,
            "清洗结果扩展名不正确",
        )
    if _is_link_or_junction(path):
        raise MarkdownCleaningProcessorError(
            MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT,
            "清洗结果路径不安全",
        )
    if expected_output_path is not None:
        _validate_expected_output_path(path, expected_output_path)

    try:
        descriptor = _open_path_nofollow(path)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            raise MarkdownCleaningProcessorError(
                MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT,
                "清洗结果不存在",
            ) from exc
        raise MarkdownCleaningProcessorError(
            MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT,
            "清洗结果读取失败",
        ) from exc

    try:
        metadata = os.fstat(descriptor)
        _validate_regular_file(metadata)
        if metadata.st_size == 0:
            raise MarkdownCleaningProcessorError(
                MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT,
                "清洗结果长度不一致",
            )
        if max_output_bytes > 0 and metadata.st_size > max_output_bytes:
            raise MarkdownCleaningProcessorError(
                MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT,
                "清洗结果超出限制",
            )
        try:
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                descriptor = -1
                data = stream.read()
        finally:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
    except Exception as exc:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if isinstance(exc, MarkdownCleaningProcessorError):
            raise
        raise MarkdownCleaningProcessorError(
            MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT,
            "清洗结果读取失败",
        ) from exc

    if hashlib.sha256(data).hexdigest() != result.output_sha256:
        raise MarkdownCleaningProcessorError(
            MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT,
            "清洗结果摘要不一致",
        )
    if len(data) != result.output_bytes:
        raise MarkdownCleaningProcessorError(
            MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT,
            "清洗结果长度不一致",
        )
    _validate_output_text_invariants(data)
    if source_text is not None:
        _validate_protected_regions(source_text, data.decode("utf-8"))


def _validate_summary(result: ProcessorResult) -> None:
    summary = result.summary
    for name in (
        "duplicate_paragraphs_removed",
        "phone_redactions",
        "id_card_redactions",
        "bank_card_redactions",
        "email_redactions",
        "ipv4_redactions",
        "formatting_changes",
    ):
        value = getattr(summary, name)
        if not isinstance(value, int) or value < 0:
            raise MarkdownCleaningProcessorError(
                MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT,
                f"清洗摘要字段无效: {name}",
            )


def _validate_output_text_invariants(data: bytes) -> None:
    if data.startswith(_BOM):
        raise MarkdownCleaningProcessorError(
            MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT,
            "清洗结果包含 BOM",
        )
    if b"\r" in data:
        raise MarkdownCleaningProcessorError(
            MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT,
            "清洗结果换行格式不正确",
        )
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise MarkdownCleaningProcessorError(
            MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT,
            "清洗结果不是有效 UTF-8",
        ) from exc
    if "\x00" in text:
        raise MarkdownCleaningProcessorError(
            MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT,
            "清洗结果包含非法字符",
        )
    if _INTERNAL_TOKEN.search(text):
        raise MarkdownCleaningProcessorError(
            MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT,
            "清洗结果包含内部标记",
        )
    if not text.endswith("\n") or text.endswith("\n\n"):
        raise MarkdownCleaningProcessorError(
            MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT,
            "清洗结果换行约束不满足",
        )


def _validate_expected_output_path(
    output_path: Path, expected_output_path: Path
) -> None:
    actual = os.path.normcase(os.path.abspath(output_path))
    expected = os.path.normcase(os.path.abspath(expected_output_path))
    if actual != expected:
        raise MarkdownCleaningProcessorError(
            MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT,
            "清洗结果路径与预期不一致",
        )


def _read_source_path(source_path: Path, expected_input_sha256: str) -> str:
    if not source_path.is_absolute():
        raise MarkdownCleaningProcessorError(
            MarkdownCleaningErrorCode.INVALID_MARKDOWN_INPUT,
            "输入路径不安全",
        )
    if _is_link_or_junction(source_path):
        raise MarkdownCleaningProcessorError(
            MarkdownCleaningErrorCode.INVALID_MARKDOWN_INPUT,
            "输入路径不安全",
        )
    try:
        descriptor = _open_path_nofollow(source_path)
        try:
            metadata = os.fstat(descriptor)
            _validate_regular_file(metadata)
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                descriptor = -1
                source_bytes = stream.read()
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    except MarkdownCleaningProcessorError as exc:
        raise MarkdownCleaningProcessorError(
            MarkdownCleaningErrorCode.INVALID_MARKDOWN_INPUT,
            "输入路径不安全",
        ) from exc
    except OSError as exc:
        raise MarkdownCleaningProcessorError(
            MarkdownCleaningErrorCode.INVALID_MARKDOWN_INPUT,
            "输入读取失败",
        ) from exc
    if hashlib.sha256(source_bytes).hexdigest() != expected_input_sha256:
        raise MarkdownCleaningProcessorError(
            MarkdownCleaningErrorCode.INVALID_MARKDOWN_INPUT,
            "输入摘要与处理结果不一致",
        )
    try:
        return source_bytes.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise MarkdownCleaningProcessorError(
            MarkdownCleaningErrorCode.INVALID_MARKDOWN_INPUT,
            "输入不是有效 UTF-8",
        ) from exc


def _validate_protected_regions(source: str, output: str) -> None:
    try:
        source_units = _protected_units(source)
        output_units = _protected_units(output)
    except Exception as exc:
        raise MarkdownCleaningProcessorError(
            MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT,
            "清洗结果保护区结构无效",
        ) from exc
    if source_units != output_units:
        raise MarkdownCleaningProcessorError(
            MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT,
            "清洗结果保护区发生变化",
        )


def _protected_units(markdown: str) -> tuple[_ProtectedUnit, ...]:
    parsed = MarkdownParserAdapter().parse(markdown)
    units: list[tuple[int, _ProtectedUnit]] = []
    for block in parsed.blocks:
        if block.block_type in {
            MarkdownBlockType.FENCED_CODE,
            MarkdownBlockType.HTML_BLOCK,
            MarkdownBlockType.THEMATIC_BREAK,
        }:
            span = block.source_span
            units.append(
                (
                    span.start,
                    _ProtectedUnit(
                        block.block_type.value, None, markdown[span.start : span.end]
                    ),
                )
            )
    for leaf in parsed.inline_leaves:
        span = leaf.source_span
        units.append(
            (
                span.start,
                _ProtectedUnit(
                    leaf.kind.value,
                    leaf.parent_block_kind.value,
                    markdown[span.start : span.end],
                ),
            )
        )
    units.sort(key=lambda item: item[0])
    return tuple(unit for _, unit in units)


def _validate_sha256_hex(context: str, value: str) -> None:
    if len(value) != 64 or any(char not in "0123456789abcdefABCDEF" for char in value):
        raise MarkdownCleaningProcessorError(
            MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT,
            f"{context}格式无效",
        )


def _open_path_nofollow(path: Path) -> int:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_BINARY", 0)
    return os.open(path, flags)


def _is_link_or_junction(path: Path) -> bool:
    return path.is_symlink() or (
        hasattr(os.path, "isjunction") and os.path.isjunction(path)
    )


def _validate_regular_file(metadata: os.stat_result) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise MarkdownCleaningProcessorError(
            MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT,
            "清洗结果文件类型不安全",
        )
