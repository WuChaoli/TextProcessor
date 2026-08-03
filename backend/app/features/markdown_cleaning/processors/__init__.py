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
from .protocol import MarkdownCleaningProcessor

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
]
