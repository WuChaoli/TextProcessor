"""Markdown cleaning processor contracts and parser helpers."""

from .errors import (
    MarkdownCleaningErrorCode,
    MarkdownCleaningProcessorError,
    map_processing_exception,
)
from .markdown_parser import (
    MarkdownBlock,
    MarkdownBlockType,
    MarkdownInlineLeaf,
    MarkdownInlineLeafType,
    MarkdownParserAdapter,
    MarkdownParserError,
    MarkdownParserErrorCode,
    MarkdownParseResult,
)
from .models import MarkdownCleaningSummary, ProcessorResult, SourceSpan
from .paragraph_dedup import ParagraphDeduplicator
from .protocol import MarkdownCleaningProcessor
from .cn_recognizers import (
    CNIDCardRecognizer,
    CNMobilePhoneRecognizer,
    IPv4AddressRecognizer,
    is_valid_cn_id_card,
    is_valid_cn_mobile_phone,
    is_valid_credit_card,
    is_valid_ipv4_address,
)
from .presidio_adapter import PresidioMarkdownRedactor, SensitiveRedactionResult, SensitiveRedactionSummary

__all__ = [
    "MarkdownCleaningErrorCode",
    "MarkdownCleaningProcessorError",
    "map_processing_exception",
    "MarkdownCleaningProcessor",
    "MarkdownCleaningSummary",
    "ProcessorResult",
    "SourceSpan",
    "MarkdownBlock",
    "MarkdownBlockType",
    "MarkdownInlineLeaf",
    "MarkdownInlineLeafType",
    "MarkdownParseResult",
    "MarkdownParserAdapter",
    "MarkdownParserError",
    "MarkdownParserErrorCode",
    "ParagraphDeduplicator",
    "CNIDCardRecognizer",
    "CNMobilePhoneRecognizer",
    "IPv4AddressRecognizer",
    "is_valid_cn_id_card",
    "is_valid_cn_mobile_phone",
    "is_valid_credit_card",
    "is_valid_ipv4_address",
    "PresidioMarkdownRedactor",
    "SensitiveRedactionResult",
    "SensitiveRedactionSummary",
]
