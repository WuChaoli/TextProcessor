"""Markdown cleaning processor contracts."""

from .errors import (
    MarkdownCleaningErrorCode,
    MarkdownCleaningProcessorError,
    map_processing_exception,
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
]
