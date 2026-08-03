from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Final

import mdformat

from app.features.markdown_cleaning.processors.errors import (
    MarkdownCleaningErrorCode,
    MarkdownCleaningProcessorError,
    map_processing_exception,
)
from app.features.markdown_cleaning.processors.markdown_parser import (
    MarkdownBlockType,
    MarkdownInlineLeafType,
    MarkdownParserAdapter,
    MarkdownParseResult,
)

_DEFAULT_FORMAT_OPTIONS: Final = {
    "wrap": "keep",
    "number": False,
    "end_of_line": "lf",
    "validate": True,
}


@dataclass(frozen=True, slots=True)
class MarkdownFormatterResult:
    text: str
    formatting_changes: int


class MarkdownFormatterAdapter:
    def __init__(
        self,
        parser: MarkdownParserAdapter | None = None,
        extensions: tuple[str, ...] = ("gfm",),
    ) -> None:
        self._parser = parser or MarkdownParserAdapter()
        self._extensions = extensions

    def format(self, markdown: str) -> MarkdownFormatterResult:
        if not isinstance(markdown, str):
            raise map_processing_exception(
                TypeError("markdown must be string"),
                MarkdownCleaningErrorCode.MARKDOWN_NORMALIZATION_FAILED,
            )
        if markdown.startswith("\ufeff"):
            raise map_processing_exception(
                ValueError("input contains BOM"),
                MarkdownCleaningErrorCode.MARKDOWN_NORMALIZATION_FAILED,
            )

        try:
            parsed_input = self._parser.parse(markdown)
            normalized = self._format_once(markdown)
            parsed_normalized = self._parser.parse(normalized)
            self._validate_output(markdown, parsed_input, normalized, parsed_normalized)

            second_pass = self._format_once(normalized)
            if second_pass != normalized:
                raise MarkdownCleaningProcessorError(
                    MarkdownCleaningErrorCode.MARKDOWN_NORMALIZATION_FAILED,
                    "Markdown 标准化失败",
                )

            changes = self._count_formatting_changes(markdown, normalized)
            return MarkdownFormatterResult(text=normalized, formatting_changes=changes)
        except MarkdownCleaningProcessorError:
            raise
        except Exception as exc:
            raise map_processing_exception(
                exc,
                MarkdownCleaningErrorCode.MARKDOWN_NORMALIZATION_FAILED,
            ) from exc

    def _format_once(self, markdown: str) -> str:
        formatted = mdformat.text(
            markdown,
            options=dict(_DEFAULT_FORMAT_OPTIONS),
            extensions=self._extensions,
            codeformatters=(),
        )

        if formatted.startswith("\ufeff"):
            raise map_processing_exception(
                ValueError("output contains BOM"),
                MarkdownCleaningErrorCode.MARKDOWN_NORMALIZATION_FAILED,
            )

        if "\r" in formatted:
            raise map_processing_exception(
                ValueError("output line endings are not LF"),
                MarkdownCleaningErrorCode.MARKDOWN_NORMALIZATION_FAILED,
            )

        if not formatted.endswith("\n"):
            raise map_processing_exception(
                ValueError("output missing terminal LF"),
                MarkdownCleaningErrorCode.MARKDOWN_NORMALIZATION_FAILED,
            )

        if formatted.endswith("\n\n"):
            raise map_processing_exception(
                ValueError("output has extra terminal LF"),
                MarkdownCleaningErrorCode.MARKDOWN_NORMALIZATION_FAILED,
            )

        return formatted

    @staticmethod
    def _count_formatting_changes(source: str, formatted: str) -> int:
        source_lines = source.splitlines(keepends=True)
        formatted_lines = formatted.splitlines(keepends=True)
        matcher = SequenceMatcher(
            autojunk=False,
            a=source_lines,
            b=formatted_lines,
        )

        change_count = 0
        last_change_end = -1
        for tag, i1, i2, _j1, _j2 in matcher.get_opcodes():
            if tag == "equal":
                continue

            if i1 > last_change_end:
                change_count += 1

            last_change_end = max(last_change_end, i2)

        return change_count

    @staticmethod
    def _normalize_protected_text(text: str) -> str:
        return text.replace("\r\n", "\n").replace("\r", "\n")

    @staticmethod
    def _collect_protected_units(
        markdown: str,
        parsed: MarkdownParseResult,
    ) -> tuple[tuple[str, str, str], ...]:
        units: list[tuple[int, str, str, str]] = []

        for block in parsed.blocks:
            if block.block_type in {
                MarkdownBlockType.FENCED_CODE,
                MarkdownBlockType.HTML_BLOCK,
            }:
                units.append(
                    (
                        block.source_span.start,
                        block.block_type.value,
                        block.block_type.value,
                        MarkdownFormatterAdapter._normalize_protected_text(
                            markdown[block.source_span.start : block.source_span.end]
                        ),
                    )
                )

        for leaf in parsed.inline_leaves:
            if leaf.kind in {
                MarkdownInlineLeafType.CODE_INLINE,
                MarkdownInlineLeafType.HTML_INLINE,
            }:
                units.append(
                    (
                        leaf.source_span.start,
                        leaf.kind.value,
                        leaf.parent_block_kind.value,
                        MarkdownFormatterAdapter._normalize_protected_text(
                            markdown[leaf.source_span.start : leaf.source_span.end]
                        ),
                    )
                )

        units.sort(key=lambda item: item[0])
        return tuple(unit[1:] for unit in units)

    @staticmethod
    def _collect_link_targets(
        markdown: str,
        parsed: MarkdownParseResult,
    ) -> tuple[str, ...]:
        return tuple(
            markdown[leaf.source_span.start : leaf.source_span.end]
            for leaf in parsed.inline_leaves
            if leaf.kind
            in {
                MarkdownInlineLeafType.LINK_DESTINATION,
                MarkdownInlineLeafType.IMAGE_DESTINATION,
            }
        )

    @staticmethod
    def _block_type_signature(parsed: MarkdownParseResult) -> tuple[MarkdownBlockType, ...]:
        return tuple(
            block.block_type
            for block in parsed.blocks
            if block.block_type is not MarkdownBlockType.BLANK
        )

    def _validate_output(
        self,
        markdown: str,
        parsed_input: MarkdownParseResult,
        normalized: str,
        parsed_normalized: MarkdownParseResult,
    ) -> None:
        if self._block_type_signature(parsed_input) != self._block_type_signature(
            parsed_normalized
        ):
            raise map_processing_exception(
                ValueError("block structure changed"),
                MarkdownCleaningErrorCode.MARKDOWN_NORMALIZATION_FAILED,
            )

        if self._collect_protected_units(markdown, parsed_input) != self._collect_protected_units(
            normalized,
            parsed_normalized,
        ):
            raise map_processing_exception(
                ValueError("protected payload changed"),
                MarkdownCleaningErrorCode.MARKDOWN_NORMALIZATION_FAILED,
            )

        if self._collect_link_targets(markdown, parsed_input) != self._collect_link_targets(
            normalized,
            parsed_normalized,
        ):
            raise map_processing_exception(
                ValueError("link destination changed"),
                MarkdownCleaningErrorCode.MARKDOWN_NORMALIZATION_FAILED,
            )
