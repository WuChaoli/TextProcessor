from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from app.features.markdown_cleaning.processors import (
    MarkdownCleaningErrorCode,
    MarkdownCleaningProcessor,
    MarkdownCleaningProcessorError,
    SourceSpan,
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
    ]
    assert str(signature.return_annotation).endswith("ProcessorResult")

    class _Dummy:
        def process(
            self,
            source_path: Path,
            destination_path: Path,
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
        input_sha256="input",
        output_sha256="output",
        contract_version="markdown_cleaning_v1",
        summary=summary,
    )
    assert result.contract_version == "markdown_cleaning_v1"


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
