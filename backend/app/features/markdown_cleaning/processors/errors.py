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


def _safe_code_message(code: MarkdownCleaningErrorCode) -> str:
    return {
        MarkdownCleaningErrorCode.INVALID_MARKDOWN_INPUT: "输入格式无效",
        MarkdownCleaningErrorCode.MARKDOWN_PARSE_FAILED: "Markdown 解析失败",
        MarkdownCleaningErrorCode.PARAGRAPH_DEDUPLICATION_FAILED: "段落去重失败",
        MarkdownCleaningErrorCode.SENSITIVE_DATA_REDACTION_FAILED: "敏感数据脱敏失败",
        MarkdownCleaningErrorCode.MARKDOWN_NORMALIZATION_FAILED: "Markdown 标准化失败",
        MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT: "处理结果文件访问失败",
        MarkdownCleaningErrorCode.PROCESSING_TIMEOUT: "处理超时",
        MarkdownCleaningErrorCode.INTERNAL_ERROR: "内部处理错误",
    }[code]


def map_processing_exception(
    exception: Exception,
    error_code: MarkdownCleaningErrorCode | None = None,
) -> MarkdownCleaningProcessorError:
    if error_code is not None:
        return MarkdownCleaningProcessorError(
            error_code,
            _safe_code_message(error_code),
        )

    if isinstance(exception, TimeoutError):
        return MarkdownCleaningProcessorError(
            MarkdownCleaningErrorCode.PROCESSING_TIMEOUT,
            _safe_code_message(MarkdownCleaningErrorCode.PROCESSING_TIMEOUT),
        )
    if isinstance(exception, ValueError):
        return MarkdownCleaningProcessorError(
            MarkdownCleaningErrorCode.INVALID_MARKDOWN_INPUT,
            _safe_code_message(MarkdownCleaningErrorCode.INVALID_MARKDOWN_INPUT),
        )
    if isinstance(exception, OSError):
        return MarkdownCleaningProcessorError(
            MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT,
            _safe_code_message(MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT),
        )
    return MarkdownCleaningProcessorError(
        MarkdownCleaningErrorCode.INTERNAL_ERROR,
        _safe_code_message(MarkdownCleaningErrorCode.INTERNAL_ERROR),
    )
