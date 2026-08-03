from __future__ import annotations

from enum import StrEnum


class MarkdownCleaningErrorCode(StrEnum):
    INVALID_MARKDOWN_INPUT = "INVALID_MARKDOWN_INPUT"
    MARKDOWN_PARSE_FAILED = "MARKDOWN_PARSE_FAILED"
    PARAGRAPH_DEDUPLICATION_FAILED = "PARAGRAPH_DEDUPLICATION_FAILED"
    SENSITIVE_DATA_REDACTION_FAILED = "SENSITIVE_DATA_REDACTION_FAILED"
    MARKDOWN_NORMALIZATION_FAILED = "MARKDOWN_NORMALIZATION_FAILED"
    INVALID_PROCESSOR_OUTPUT = "INVALID_PROCESSOR_OUTPUT"
    PROCESSING_TIMEOUT = "PROCESSING_TIMEOUT"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class MarkdownCleaningProcessorError(RuntimeError):
    def __init__(self, code: MarkdownCleaningErrorCode, safe_message: str) -> None:
        super().__init__(f"{code}: {safe_message}")
        self.code = code
        self.safe_message = safe_message


def map_processing_exception(
    exception: Exception,
) -> MarkdownCleaningProcessorError:
    if isinstance(exception, TimeoutError):
        return MarkdownCleaningProcessorError(
            MarkdownCleaningErrorCode.PROCESSING_TIMEOUT,
            "处理超时",
        )
    if isinstance(exception, ValueError):
        return MarkdownCleaningProcessorError(
            MarkdownCleaningErrorCode.INVALID_MARKDOWN_INPUT,
            "输入无效",
        )
    if isinstance(exception, OSError):
        return MarkdownCleaningProcessorError(
            MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT,
            "处理结果文件访问失败",
        )
    return MarkdownCleaningProcessorError(
        MarkdownCleaningErrorCode.INTERNAL_ERROR,
        "内部处理错误",
    )
