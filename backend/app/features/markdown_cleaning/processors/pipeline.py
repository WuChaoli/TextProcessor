from __future__ import annotations

import hashlib
from dataclasses import asdict
from pathlib import Path

from app.features.markdown_cleaning.processors.errors import (
    MarkdownCleaningErrorCode,
    MarkdownCleaningProcessorError,
    map_processing_exception,
)
from app.features.markdown_cleaning.processors.markdown_formatter import (
    MarkdownFormatterAdapter,
)
from app.features.markdown_cleaning.processors.markdown_parser import (
    MarkdownParserAdapter,
)
from app.features.markdown_cleaning.processors.models import (
    MarkdownCleaningSummary,
    ProcessorResult,
)
from app.features.markdown_cleaning.processors.paragraph_dedup import (
    ParagraphDeduplicator,
)
from app.features.markdown_cleaning.processors.presidio_adapter import (
    PresidioMarkdownRedactor,
)


class MarkdownCleaningPipeline:
    def __init__(
        self,
        *,
        parser: MarkdownParserAdapter | None = None,
        deduplicator: ParagraphDeduplicator | None = None,
        redactor: PresidioMarkdownRedactor | None = None,
        formatter: MarkdownFormatterAdapter | None = None,
    ) -> None:
        self._parser = parser or MarkdownParserAdapter()
        self._deduplicator = deduplicator or ParagraphDeduplicator(parser=self._parser)
        self._redactor = redactor or PresidioMarkdownRedactor()
        self._formatter = formatter or MarkdownFormatterAdapter(parser=self._parser)

    def process(self, source_path: Path, destination_path: Path) -> ProcessorResult:
        source_bytes = source_path.read_bytes()
        input_sha256 = hashlib.sha256(source_bytes).hexdigest()
        input_text = source_bytes.decode("utf-8")

        if input_text.startswith("\ufeff"):
            raise map_processing_exception(
                ValueError("input must not contain BOM"),
                MarkdownCleaningErrorCode.INVALID_MARKDOWN_INPUT,
            )

        try:
            deduplicated_text, duplicate_count = self._deduplicator.deduplicate(
                input_text,
            )

            parsed_after_dedup = self._parser.parse(deduplicated_text)
            redaction = self._redactor.redact(
                deduplicated_text,
                parsed_after_dedup.protected_spans,
            )

            formatted = self._formatter.format(redaction.text)
        except MarkdownCleaningProcessorError:
            raise
        except Exception as exc:
            code = _map_pipeline_error_code(exc)
            raise map_processing_exception(exc, code) from exc

        output_text = formatted.text.replace("\r\n", "\n").replace("\r", "\n")
        if not output_text.endswith("\n"):
            output_text += "\n"

        destination_path.parent.mkdir(parents=True, exist_ok=True)
        destination_path.write_text(output_text, encoding="utf-8")

        output_bytes = destination_path.read_bytes()
        if not output_bytes:
            raise map_processing_exception(
                ValueError("output is empty"),
                MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT,
            )

        output_sha256 = hashlib.sha256(output_bytes).hexdigest()
        summary = MarkdownCleaningSummary(
            duplicate_paragraphs_removed=duplicate_count,
            phone_redactions=redaction.summary.phone,
            id_card_redactions=redaction.summary.id_card,
            bank_card_redactions=redaction.summary.bank_card,
            email_redactions=redaction.summary.email,
            ipv4_redactions=redaction.summary.ipv4,
            formatting_changes=formatted.formatting_changes,
        )

        if any(value < 0 for value in asdict(summary).values()):
            raise map_processing_exception(
                ValueError("summary contains negative counts"),
                MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT,
            )

        return ProcessorResult(
            output_path=destination_path,
            input_sha256=input_sha256,
            output_sha256=output_sha256,
            contract_version="markdown_cleaning_v1",
            summary=summary,
            input_bytes=len(source_bytes),
            output_bytes=len(output_bytes),
        )


def _map_pipeline_error_code(exc: Exception) -> MarkdownCleaningErrorCode:
    message = str(exc).lower()
    if "duplicate" in message and "paragraph" in message:
        return MarkdownCleaningErrorCode.PARAGRAPH_DEDUPLICATION_FAILED
    if "redact" in message or "token" in message or "sensitive" in message:
        return MarkdownCleaningErrorCode.SENSITIVE_DATA_REDACTION_FAILED
    if "format" in message or "markdown" in message:
        return MarkdownCleaningErrorCode.MARKDOWN_NORMALIZATION_FAILED
    return MarkdownCleaningErrorCode.MARKDOWN_PARSE_FAILED
