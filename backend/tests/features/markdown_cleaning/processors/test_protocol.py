from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError
from datetime import datetime
from pathlib import Path

import pytest

from app.features.markdown_cleaning.processors import (
    MarkdownCleaningErrorCode,
    MarkdownCleaningProcessor,
    MarkdownCleaningProcessorError,
    SourceSpan,
    map_processing_exception,
)
from app.features.markdown_cleaning.processors.models import (
    MarkdownCleaningSummary,
    ProcessorResult,
)
from app.features.markdown_cleaning.processors.protocol import (
    MarkdownCleaningProcessor as ProcessorProtocol,
)


def test_markdown_cleaning_processor_protocol_has_expected_signature() -> None:
    signature = inspect.signature(MarkdownCleaningProcessor.process)
    params = list(signature.parameters.values())

    assert [param.name for param in params] == [
        "self",
        "source_path",
        "destination_path",
        "deadline",
    ]
    assert params[-1].kind is inspect.Parameter.KEYWORD_ONLY
    assert params[-1].default is None
    assert "datetime" in str(params[-1].annotation)
    assert str(signature.return_annotation).endswith("ProcessorResult")

    class _Dummy:
        def process(
            self,
            source_path: Path,
            destination_path: Path,
            *,
            deadline: datetime | None = None,
        ) -> ProcessorResult:
            raise NotImplementedError

    processor = _Dummy()
    assert isinstance(processor, ProcessorProtocol)


def test_result_and_span_models_are_immutable_and_have_all_summary_fields() -> None:
    span = SourceSpan(start=0, end=10)
    assert span.start == 0
    assert span.end == 10
    with pytest.raises(FrozenInstanceError):
        span.start = 1

    summary = MarkdownCleaningSummary(
        duplicate_paragraphs_removed=1,
        phone_redactions=2,
        id_card_redactions=3,
        bank_card_redactions=4,
        email_redactions=5,
        ipv4_redactions=6,
        formatting_changes=7,
    )
    assert summary.duplicate_paragraphs_removed == 1
    assert summary.phone_redactions == 2
    assert summary.id_card_redactions == 3
    assert summary.bank_card_redactions == 4
    assert summary.email_redactions == 5
    assert summary.ipv4_redactions == 6
    assert summary.formatting_changes == 7
    with pytest.raises(FrozenInstanceError):
        summary.formatting_changes = 8

    result = ProcessorResult(
        output_path=Path("/tmp/output.md"),
        input_sha256="00" * 32,
        output_sha256="ff" * 32,
        contract_version="markdown_cleaning_v1",
        summary=summary,
    )
    assert result.contract_version == "markdown_cleaning_v1"
    with pytest.raises(ValueError):
        MarkdownCleaningSummary(
            duplicate_paragraphs_removed=-1,
            phone_redactions=0,
            id_card_redactions=0,
            bank_card_redactions=0,
            email_redactions=0,
            ipv4_redactions=0,
            formatting_changes=0,
        )
    with pytest.raises(ValueError):
        ProcessorResult(
            output_path=Path("/tmp/output.md"),
            input_sha256="bad",
            output_sha256="ff" * 32,
            contract_version="markdown_cleaning_v1",
            summary=summary,
        )
    with pytest.raises(ValueError):
        ProcessorResult(
            output_path=Path("/tmp/output.md"),
            input_sha256="00" * 32,
            output_sha256="nothex" * 10 + "0",
            contract_version="markdown_cleaning_v1",
            summary=summary,
        )
    with pytest.raises(ValueError):
        ProcessorResult(
            output_path=Path("/tmp/output.md"),
            input_sha256="00" * 32,
            output_sha256="ff" * 32,
            contract_version="markdown_cleaning_v1",
            summary=summary,
            input_bytes=-1,
        )
    with pytest.raises(ValueError):
        ProcessorResult(
            output_path=Path("/tmp/output.md"),
            input_sha256="00" * 32,
            output_sha256="ff" * 32,
            contract_version="markdown_cleaning_v1",
            summary=summary,
            output_bytes=-1,
        )


def test_stable_error_codes_and_exception_mapping() -> None:
    expected_codes = {
        MarkdownCleaningErrorCode.INVALID_MARKDOWN_INPUT,
        MarkdownCleaningErrorCode.MARKDOWN_PARSE_FAILED,
        MarkdownCleaningErrorCode.PARAGRAPH_DEDUPLICATION_FAILED,
        MarkdownCleaningErrorCode.SENSITIVE_DATA_REDACTION_FAILED,
        MarkdownCleaningErrorCode.MARKDOWN_NORMALIZATION_FAILED,
        MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT,
        MarkdownCleaningErrorCode.PROCESSING_TIMEOUT,
        MarkdownCleaningErrorCode.INTERNAL_ERROR,
    }
    assert set(MarkdownCleaningErrorCode) == expected_codes

    error = MarkdownCleaningProcessorError(
        MarkdownCleaningErrorCode.INVALID_MARKDOWN_INPUT,
        "输入不合法",
    )
    assert error.code is MarkdownCleaningErrorCode.INVALID_MARKDOWN_INPUT
    assert error.safe_message == "输入不合法"
    assert "输入不合法" in str(error)


def test_map_processing_exception_exposes_stage_code_without_leaking_exception_message() -> None:
    default_invalid_input = map_processing_exception(ValueError("user_secret=abc123"))
    assert default_invalid_input.code is MarkdownCleaningErrorCode.INVALID_MARKDOWN_INPUT
    assert "abc123" not in default_invalid_input.safe_message
    assert "user_secret" not in default_invalid_input.safe_message

    secret_exception = ValueError("user_secret=abc123")
    for code in (
        MarkdownCleaningErrorCode.MARKDOWN_PARSE_FAILED,
        MarkdownCleaningErrorCode.PARAGRAPH_DEDUPLICATION_FAILED,
        MarkdownCleaningErrorCode.SENSITIVE_DATA_REDACTION_FAILED,
        MarkdownCleaningErrorCode.MARKDOWN_NORMALIZATION_FAILED,
    ):
        mapped = map_processing_exception(secret_exception, code)
        assert mapped.code is code
        assert "abc123" not in mapped.safe_message
        assert "user_secret" not in mapped.safe_message
        assert mapped.safe_message != ""

    timeout_error = map_processing_exception(TimeoutError("timeout"))
    assert timeout_error.code is MarkdownCleaningErrorCode.PROCESSING_TIMEOUT
    assert "timeout" not in timeout_error.safe_message

    io_error = map_processing_exception(OSError("disk secret"))
    assert io_error.code is MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT
    assert "disk secret" not in io_error.safe_message

    unknown = map_processing_exception(RuntimeError("x"))
    assert unknown.code is MarkdownCleaningErrorCode.INTERNAL_ERROR
    assert "x" not in unknown.safe_message
