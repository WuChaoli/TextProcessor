import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from markdown_it import MarkdownIt

from app.features.markdown_cleaning.processors.models import SourceSpan
from app.features.markdown_cleaning.processors.source_spans import (
    build_line_offsets,
    merge_source_spans,
    source_span_from_line_map,
)


class MarkdownParserErrorCode(StrEnum):
    INVALID_MARKDOWN_INPUT = "INVALID_MARKDOWN_INPUT"
    MARKDOWN_PARSE_FAILED = "MARKDOWN_PARSE_FAILED"


class MarkdownParserError(ValueError):
    def __init__(self, code: MarkdownParserErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class MarkdownBlockType(StrEnum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list_item"
    BLOCKQUOTE = "blockquote"
    TABLE = "table"
    FENCED_CODE = "fenced_code"
    INLINE_CODE = "inline_code"
    HTML_BLOCK = "html_block"
    HTML_INLINE = "html_inline"
    LINK_DESTINATION = "link_destination"
    THEMATIC_BREAK = "thematic_break"
    BLANK = "blank"


@dataclass(frozen=True, slots=True)
class MarkdownBlock:
    block_type: MarkdownBlockType
    line_start: int
    line_end: int
    source_span: SourceSpan


@dataclass(frozen=True, slots=True)
class MarkdownParseResult:
    blocks: tuple[MarkdownBlock, ...]
    protected_spans: tuple[SourceSpan, ...]


_BLOCK_OPEN_TO_TYPE: dict[str, MarkdownBlockType] = {
    "heading_open": MarkdownBlockType.HEADING,
    "paragraph_open": MarkdownBlockType.PARAGRAPH,
    "list_item_open": MarkdownBlockType.LIST_ITEM,
    "blockquote_open": MarkdownBlockType.BLOCKQUOTE,
    "table_open": MarkdownBlockType.TABLE,
}

_BLOCK_CLOSE_TO_OPEN: dict[str, MarkdownBlockType] = {
    "heading_close": MarkdownBlockType.HEADING,
    "paragraph_close": MarkdownBlockType.PARAGRAPH,
    "list_item_close": MarkdownBlockType.LIST_ITEM,
    "blockquote_close": MarkdownBlockType.BLOCKQUOTE,
    "table_close": MarkdownBlockType.TABLE,
}

_FENCED_CODE_OPEN = re.compile(
    r"^[ \t]{0,3}(?P<marker>`{3,}|~{3,})(?P<tail>.*)$",
)
_FENCED_CODE_CLOSE_PREFIX = re.compile(
    r"^[ \t]{0,3}(?P<marker>`{3,}|~{3,})(?P<trailing>[ \t]*)$",
)
_INLINE_CODE_ESCAPE_PATTERN = re.compile(r"(?<!`)(`+)([^`\\n]+?)\1(?!`)")


class MarkdownParserAdapter:
    def __init__(self, markdown_it: MarkdownIt | None = None) -> None:
        self._markdown_it = markdown_it or MarkdownIt("commonmark").enable("table")

    def parse_file(self, source: Path) -> MarkdownParseResult:
        markdown = source.read_text(encoding="utf-8")
        return self.parse(markdown)

    def parse(self, markdown: str) -> MarkdownParseResult:
        self._validate_fenced_code_blocks_closed(markdown)

        try:
            tokens = self._markdown_it.parse(markdown)
        except Exception as exc:
            raise MarkdownParserError(
                MarkdownParserErrorCode.MARKDOWN_PARSE_FAILED,
                "Markdown parser execution failed",
            ) from exc

        line_offsets = build_line_offsets(markdown)
        lines = markdown.splitlines(keepends=True)

        blocks: list[MarkdownBlock] = []
        protected: list[SourceSpan] = []
        open_stack: list[tuple[MarkdownBlockType, int, int]] = []

        try:
            for token in tokens:
                token_type = token.type

                if token_type in {"fence", "hr", "html_block"}:
                    token_map = token.map
                    if token_map is None or len(token_map) != 2:
                        if token_type == "fence":
                            raise MarkdownParserError(
                                MarkdownParserErrorCode.MARKDOWN_PARSE_FAILED,
                                "fenced code block cannot map to source offsets",
                            )
                        continue

                    span = source_span_from_line_map(token_map, line_offsets)
                    if span is None or span.end <= span.start:
                        if token_type == "fence":
                            raise MarkdownParserError(
                                MarkdownParserErrorCode.MARKDOWN_PARSE_FAILED,
                                "fenced code block cannot map to source offsets",
                            )
                        continue

                    if token_type == "fence":
                        blocks.append(
                            MarkdownBlock(
                                block_type=MarkdownBlockType.FENCED_CODE,
                                line_start=token_map[0],
                                line_end=token_map[1],
                                source_span=span,
                            )
                        )
                        protected.append(span)
                    elif token_type == "html_block":
                        blocks.append(
                            MarkdownBlock(
                                block_type=MarkdownBlockType.HTML_BLOCK,
                                line_start=token_map[0],
                                line_end=token_map[1],
                                source_span=span,
                            )
                        )
                        protected.append(span)
                    else:
                        blocks.append(
                            MarkdownBlock(
                                block_type=MarkdownBlockType.THEMATIC_BREAK,
                                line_start=token_map[0],
                                line_end=token_map[1],
                                source_span=span,
                            )
                        )
                    continue

                if token.nesting == 1 and token_type in _BLOCK_OPEN_TO_TYPE:
                    token_map = token.map
                    if token_map is not None and len(token_map) == 2:
                        open_stack.append(
                            (
                                _BLOCK_OPEN_TO_TYPE[token_type],
                                token_map[0],
                                token_map[1],
                            )
                        )
                    continue

                if token.nesting == -1:
                    block_type = _BLOCK_CLOSE_TO_OPEN.get(token_type)
                    if block_type is None:
                        continue

                    while open_stack:
                        open_type, line_start, line_end = open_stack.pop()
                        if open_type == block_type:
                            blocks.append(
                                MarkdownBlock(
                                    block_type=open_type,
                                    line_start=line_start,
                                    line_end=line_end,
                                    source_span=source_span_from_line_map(
                                        [line_start, line_end],
                                        line_offsets,
                                    )
                                    or SourceSpan(0, 0),
                                )
                            )
                            break

                if token_type == "inline" and token.children:
                    inline_map = token.map
                    if inline_map is None or len(inline_map) != 2:
                        raise MarkdownParserError(
                            MarkdownParserErrorCode.MARKDOWN_PARSE_FAILED,
                            "inline token cannot map to source line offsets",
                        )

                    try:
                        line_start = inline_map[0]
                    except TypeError as exc:
                        raise MarkdownParserError(
                            MarkdownParserErrorCode.MARKDOWN_PARSE_FAILED,
                            "inline token line map is invalid",
                        ) from exc

                    base_offset = line_offsets[line_start] if line_start >= 0 else 0
                    protected.extend(
                        self._collect_inline_protected_spans(
                            token.children,
                            token.content,
                            base_offset,
                        )
                    )

            blocks.extend(self._blank_blocks(lines, line_offsets))
            blocks.sort(key=lambda block: (block.source_span.start, block.source_span.end))
            protected_sorted = merge_source_spans(protected)
        except MarkdownParserError:
            raise
        except Exception as exc:
            raise MarkdownParserError(
                MarkdownParserErrorCode.MARKDOWN_PARSE_FAILED,
                "Markdown parser execution failed",
            ) from exc

        return MarkdownParseResult(blocks=tuple(blocks), protected_spans=protected_sorted)

    def _collect_inline_protected_spans(
        self,
        children: Sequence[object],
        inline_markdown: str,
        base_offset: int,
    ) -> list[SourceSpan]:
        spans: list[SourceSpan] = []
        cursor = 0

        for child in children:
            child_type = getattr(child, "type", None)
            if child_type == "code_inline":
                span = self._extract_code_inline_span(
                    child,
                    inline_markdown,
                    cursor,
                    base_offset,
                )
                if span is None:
                    continue
                spans.append(span)
                cursor = span.end - base_offset
                continue

            if child_type == "html_inline":
                content = getattr(child, "content", "")
                if content:
                    index = inline_markdown.find(content, cursor)
                    if index >= 0:
                        spans.append(
                            SourceSpan(
                                start=base_offset + index,
                                end=base_offset + index + len(content),
                            )
                        )
                        cursor = index + len(content)
                continue

            if child_type == "link_open":
                destination = self._get_child_attribute(child, "href")
                if destination:
                    span = self._extract_destination_span(
                        inline_markdown,
                        cursor,
                        destination,
                    )
                    if span is not None:
                        protected = SourceSpan(
                            start=base_offset + span.start,
                            end=base_offset + span.end,
                        )
                        spans.append(protected)
                        cursor = span.end
                continue

            if child_type == "image":
                destination = self._get_child_attribute(child, "src")
                if destination:
                    span = self._extract_destination_span(
                        inline_markdown,
                        cursor,
                        destination,
                    )
                    if span is not None:
                        protected = SourceSpan(
                            start=base_offset + span.start,
                            end=base_offset + span.end,
                        )
                        spans.append(protected)
                        cursor = span.end

        return spans

    @staticmethod
    def _extract_code_inline_span(
        child: object,
        inline_markdown: str,
        cursor: int,
        base_offset: int,
    ) -> SourceSpan | None:
        markup = getattr(child, "markup", "`")
        content = getattr(child, "content", "")
        if not isinstance(markup, str) or not isinstance(content, str):
            return None

        pattern = re.escape(markup) + re.escape(content) + re.escape(markup)
        match = re.compile(pattern).search(inline_markdown, cursor)
        if match is None:
            fallback = _INLINE_CODE_ESCAPE_PATTERN.search(
                inline_markdown,
                cursor,
            )
            if fallback is None:
                return None
            match = fallback

        return SourceSpan(
            start=base_offset + match.start(),
            end=base_offset + match.end(),
        )

    @staticmethod
    def _extract_destination_span(
        inline_markdown: str,
        cursor: int,
        destination: str,
    ) -> SourceSpan | None:
        if not destination:
            return None

        # Parse markdown link/image syntax from cursor and map destination span precisely.
        anchor = cursor
        while anchor + 1 < len(inline_markdown):
            if inline_markdown[anchor] == ")" or inline_markdown[anchor] == "\n":
                return None
            if inline_markdown[anchor] != "]":
                anchor += 1
                continue
            if anchor + 1 >= len(inline_markdown) or inline_markdown[anchor + 1] != "(":
                anchor += 1
                continue

            open_paren = anchor + 2
            while open_paren < len(inline_markdown) and inline_markdown[
                open_paren
            ].isspace():
                open_paren += 1

            if open_paren >= len(inline_markdown):
                return None

            if inline_markdown[open_paren] == "<":
                close = inline_markdown.find(">", open_paren + 1)
                if close < 0:
                    return None
                start = open_paren + 1
                end = close
                tail = close + 1
                while tail < len(inline_markdown) and inline_markdown[tail].isspace():
                    tail += 1
                if tail < len(inline_markdown) and inline_markdown[tail] == ")":
                    return SourceSpan(start=start, end=end)
                if destination == inline_markdown[start:end]:
                    return SourceSpan(start=start, end=end)
            else:
                start = open_paren
                end = open_paren
                while end < len(inline_markdown) and inline_markdown[end] not in (
                    " ",
                    "\t",
                    ")",
                    "\n",
                    "\r",
                ):
                    end += 1
                if destination == inline_markdown[start:end]:
                    if end < len(inline_markdown) and inline_markdown[end] == " ":
                        maybe = end
                        while maybe < len(inline_markdown) and inline_markdown[
                            maybe
                        ].isspace():
                            maybe += 1
                        if maybe < len(inline_markdown) and inline_markdown[maybe] == ")":
                            return SourceSpan(start=start, end=end)
                    elif end < len(inline_markdown) and inline_markdown[end] == ")":
                        return SourceSpan(start=start, end=end)

            anchor += 1
        return None

    @staticmethod
    def _get_child_attribute(child: object, key: str) -> str:
        attrs = getattr(child, "attrs", None)
        if not attrs:
            return ""
        if isinstance(attrs, dict):
            return str(attrs.get(key, ""))
        if isinstance(attrs, list):
            for name, value in attrs:
                if name == key:
                    return str(value)
        return ""

    @staticmethod
    def _blank_blocks(
        lines: list[str],
        line_offsets: tuple[int, ...],
    ) -> list[MarkdownBlock]:
        if not lines:
            return []

        blanks: list[MarkdownBlock] = []
        line_count = len(lines)
        current = 0

        while current < line_count:
            if lines[current].strip():
                current += 1
                continue

            blank_start = current
            while current < line_count and not lines[current].strip():
                current += 1
            blank_end = current

            span = source_span_from_line_map(
                [blank_start, blank_end],
                line_offsets,
            )
            if span and span.start != span.end:
                blanks.append(
                    MarkdownBlock(
                        block_type=MarkdownBlockType.BLANK,
                        line_start=blank_start,
                        line_end=blank_end,
                        source_span=span,
                    )
                )

        return blanks

    @staticmethod
    def _validate_fenced_code_blocks_closed(markdown: str) -> None:
        open_marker: str | None = None

        for line in markdown.splitlines():
            if open_marker is None:
                match_open = _FENCED_CODE_OPEN.match(line)
                if match_open is None:
                    continue
                open_marker = match_open.group("marker")
                continue

            match_close = _FENCED_CODE_CLOSE_PREFIX.match(line)
            if match_close is None:
                continue

            close_marker = match_close.group("marker")
            if close_marker[0] != open_marker[0]:
                continue
            if len(close_marker) < len(open_marker):
                continue

            open_marker = None

        if open_marker is not None:
            raise MarkdownParserError(
                MarkdownParserErrorCode.INVALID_MARKDOWN_INPUT,
                "unclosed fenced code block",
            )
