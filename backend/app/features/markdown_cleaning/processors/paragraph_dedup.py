"""Markdown paragraph-level deduplication processor."""

from __future__ import annotations

import re

from app.features.markdown_cleaning.processors.markdown_parser import (
    MarkdownBlock,
    MarkdownBlockType,
    MarkdownParserAdapter,
)

_SOFTBREAK_PATTERN = re.compile(r"\r\n|\r|\n")
_WHITESPACE_PATTERN = re.compile(r"[ \t]+")


class ParagraphDeduplicator:
    """Keep first occurrence of each normal paragraph and remove later duplicates."""

    def __init__(self, parser: MarkdownParserAdapter | None = None) -> None:
        self._parser = parser or MarkdownParserAdapter()

    def deduplicate(self, markdown: str) -> tuple[str, int]:
        parsed = self._parser.parse(markdown)
        blocks = parsed.blocks

        seen_keys: set[str] = set()
        removed: set[int] = set()
        duplicate_count = 0

        for index, block in enumerate(blocks):
            if block.block_type != MarkdownBlockType.PARAGRAPH:
                continue

            paragraph = markdown[block.source_span.start : block.source_span.end]
            key = self._normalize_for_key(paragraph)
            if key in seen_keys:
                removed.add(index)
                duplicate_count += 1
                continue
            seen_keys.add(key)

        selected_blocks = self._filter_blocks_with_controlled_blanks(blocks, removed)
        selected_blocks.sort(key=lambda block: (block.source_span.start, block.source_span.end))
        if not selected_blocks:
            return "", duplicate_count

        return "".join(
            markdown[block.source_span.start : block.source_span.end]
            for block in selected_blocks
        ), duplicate_count

    @staticmethod
    def _normalize_for_key(markdown: str) -> str:
        normalized = _SOFTBREAK_PATTERN.sub(" ", markdown.strip())
        normalized = _WHITESPACE_PATTERN.sub(" ", normalized)
        return normalized.strip()

    @staticmethod
    def _filter_blocks_with_controlled_blanks(
        blocks: tuple[MarkdownBlock, ...],
        removed: set[int],
    ) -> list[MarkdownBlock]:
        if not removed:
            return list(blocks)

        paragraph_indexes = {
            index for index in removed if blocks[index].block_type == MarkdownBlockType.PARAGRAPH
        }
        if not paragraph_indexes:
            return list(blocks)

        kept_indices = [
            index
            for index, block in enumerate(blocks)
            if index not in removed
            and block.block_type != MarkdownBlockType.BLANK
        ]

        if not kept_indices:
            # keep only non-removed non-blank? there is no such block
            return []

        filtered: list[MarkdownBlock] = []
        for idx, block in enumerate(blocks):
            if idx in removed:
                continue
            if block.block_type != MarkdownBlockType.BLANK:
                filtered.append(block)
                continue

            has_removed_paragraph_between = ParagraphDeduplicator._has_removed_paragraph_between(
                idx,
                blocks,
                paragraph_indexes,
                kept_indices,
            )
            if not has_removed_paragraph_between:
                filtered.append(block)
                continue

            if not ParagraphDeduplicator._blank_already_kept(filtered):
                filtered.append(block)

        return filtered

    @staticmethod
    def _has_removed_paragraph_between(
        blank_index: int,
        blocks: tuple[MarkdownBlock, ...],
        removed_paragraph_indexes: set[int],
        kept_indices: list[int],
    ) -> bool:
        previous_kept = max((index for index in kept_indices if index < blank_index), default=None)
        next_kept = min((index for index in kept_indices if index > blank_index), default=None)

        if previous_kept is None or next_kept is None:
            return False

        for index in range(previous_kept + 1, next_kept):
            if index in removed_paragraph_indexes:
                return True
        return False

    @staticmethod
    def _blank_already_kept(filtered: list[MarkdownBlock]) -> bool:
        if not filtered:
            return False
        return filtered[-1].block_type == MarkdownBlockType.BLANK
