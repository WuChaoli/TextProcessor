"""Markdown paragraph-level deduplication processor."""

from __future__ import annotations

import re

from app.features.markdown_cleaning.processors.markdown_parser import (
    MarkdownBlock,
    MarkdownBlockType,
    MarkdownParserAdapter,
)
from app.features.markdown_cleaning.processors.models import SourceSpan

_SOFTBREAK_PATTERN = re.compile(r"\r\n|\r|\n")
_WHITESPACE_PATTERN = re.compile(r"[ \t]+")
_CONTAINER_BLOCK_TYPES = {
    MarkdownBlockType.LIST_ITEM,
    MarkdownBlockType.BLOCKQUOTE,
    MarkdownBlockType.TABLE,
}


class ParagraphDeduplicator:
    """Deduplicate only top-level paragraph blocks while preserving document order."""

    def __init__(self, parser: MarkdownParserAdapter | None = None) -> None:
        self._parser = parser or MarkdownParserAdapter()

    def deduplicate(self, markdown: str) -> tuple[str, int]:
        parsed = self._parser.parse(markdown)
        blocks = parsed.blocks

        seen_keys: set[str] = set()
        duplicate_count = 0
        delete_spans: list[SourceSpan] = []

        for index, block in enumerate(blocks):
            if block.block_type != MarkdownBlockType.PARAGRAPH:
                continue

            if not self._is_top_level_paragraph(block, blocks):
                continue

            paragraph = markdown[block.source_span.start : block.source_span.end]
            key = self._normalize_for_key(paragraph)

            if key in seen_keys:
                duplicate_count += 1
                delete_spans.append(
                    self._build_duplicate_delete_span(blocks=blocks, index=index, block=block)
                )
                continue

            seen_keys.add(key)

        if not delete_spans:
            return markdown, duplicate_count

        return self._delete_spans(markdown, self._assert_disjoint(delete_spans)), duplicate_count

    @staticmethod
    def _normalize_for_key(markdown: str) -> str:
        normalized = _SOFTBREAK_PATTERN.sub(" ", markdown)
        normalized = _WHITESPACE_PATTERN.sub(" ", normalized)
        return normalized.strip()

    @staticmethod
    def _is_top_level_paragraph(
        block: MarkdownBlock,
        blocks: tuple[MarkdownBlock, ...],
    ) -> bool:
        for container in blocks:
            if container.block_type not in _CONTAINER_BLOCK_TYPES:
                continue
            if container is block:
                continue
            if (
                container.source_span.start <= block.source_span.start
                and container.source_span.end >= block.source_span.end
            ):
                return False
        return True

    @staticmethod
    def _build_duplicate_delete_span(
        blocks: tuple[MarkdownBlock, ...],
        index: int,
        block: MarkdownBlock,
    ) -> SourceSpan:
        start = block.source_span.start
        end = block.source_span.end

        if index + 1 < len(blocks) and blocks[index + 1].block_type == MarkdownBlockType.BLANK:
            end = blocks[index + 1].source_span.end

        return SourceSpan(start=start, end=end)

    @staticmethod
    def _assert_disjoint(delete_spans: list[SourceSpan]) -> list[SourceSpan]:
        ordered = sorted(delete_spans, key=lambda span: span.start)
        for previous, current in zip(ordered, ordered[1:], strict=False):
            if current.start < previous.end:
                raise ValueError("duplicate paragraph delete spans overlap")
        return ordered

    @staticmethod
    def _delete_spans(text: str, spans: list[SourceSpan]) -> str:
        deduplicated = text
        for span in reversed(spans):
            deduplicated = deduplicated[:span.start] + deduplicated[span.end :]
        return deduplicated
