import hashlib

from app.features.markdown_cleaning.processors.errors import (
    MarkdownCleaningErrorCode,
    MarkdownCleaningProcessorError,
)
from app.features.markdown_cleaning.processors.models import ProcessorResult


def validate_pipeline_output(
    result: ProcessorResult,
    *,
    expected_input_sha256: str,
    max_output_bytes: int,
) -> ProcessorResult:
    _validate_contract_version(result)
    _validate_input(result, expected_input_sha256)
    _validate_output_blob(result, max_output_bytes)
    _validate_summary(result)
    return result


class MarkdownCleaningOutputValidator:
    def validate(
        self,
        result: ProcessorResult,
        *,
        expected_input_sha256: str,
        max_output_bytes: int,
    ) -> ProcessorResult:
        return validate_pipeline_output(
            result,
            expected_input_sha256=expected_input_sha256,
            max_output_bytes=max_output_bytes,
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
    if not path.is_file():
        raise MarkdownCleaningProcessorError(
            MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT,
            "清洗结果文件不存在",
        )
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise MarkdownCleaningProcessorError(
            MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT,
            "清洗结果读取失败",
        ) from exc
    if len(data) != result.output_bytes:
        raise MarkdownCleaningProcessorError(
            MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT,
            "清洗结果长度不一致",
        )
    if hashlib.sha256(data).hexdigest() != result.output_sha256:
        raise MarkdownCleaningProcessorError(
            MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT,
            "清洗结果摘要不一致",
        )


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
