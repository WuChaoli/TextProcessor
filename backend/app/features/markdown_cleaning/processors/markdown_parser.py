import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from markdown_it import MarkdownIt


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
    THEMATIC_BREAK = "thematic_break"
    BLANK = "blank"


@dataclass(frozen=True, slots=True)
class SourceSpan:
    start: int
    end: int


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
    r"^(?P<indent>[ \t]{0,3})(?P<marker>`{3,}|~{3,})(?P<tail>.*)$",
)

_FENCED_CODE_CLOSE_PREFIX = re.compile(
    r"^[ \t]{0,3}(?P<marker>`{3,}|~{3,})(?P<trailing>[ \t]*)$",
)

_INLINE_CODE_PATTERN = re.compile(r"(?<!`)(`+)([^`\n]+?)\1(?!`)")


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

        lines = markdown.splitlines(keepends=True)
        line_offsets = self._line_offsets(lines)

        blocks: list[MarkdownBlock] = []
        protected: list[SourceSpan] = []

        open_stack: list[tuple[MarkdownBlockType, int, int]] = []

        for token in tokens:
            token_type = token.type

            if token_type in {"fence", "hr", "html_block"}:
                span = self._token_map_to_char_span(token.map, line_offsets)
                if span is None:
                    continue

                if token_type == "fence":
                    blocks.append(
                        MarkdownBlock(
                            block_type=MarkdownBlockType.FENCED_CODE,
                            line_start=token.map[0],
                            line_end=token.map[1],
                            source_span=span,
                        )
                    )
                    protected.append(span)
                elif token_type == "html_block":
                    blocks.append(
                        MarkdownBlock(
                            block_type=MarkdownBlockType.HTML_BLOCK,
                            line_start=token.map[0],
                            line_end=token.map[1],
                            source_span=span,
                        )
                    )
                    protected.append(span)
                else:
                    blocks.append(
                        MarkdownBlock(
                            block_type=MarkdownBlockType.THEMATIC_BREAK,
                            line_start=token.map[0],
                            line_end=token.map[1],
                            source_span=span,
                        )
                    )
                continue

            if token.nesting == 1:
                if token_type in _BLOCK_OPEN_TO_TYPE and token.map is not None:
                    open_stack.append(
                        (
                            _BLOCK_OPEN_TO_TYPE[token_type],
                            token.map[0],
                            token.map[1],
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
                                source_span=self._line_span_to_char_span(
                                    line_start,
                                    line_end,
                                    line_offsets,
                                ),
                            )
                        )
                        break

            if token_type == "inline" and token.children:
                inline_code_spans = self._extract_inline_code_spans(
                    token.children,
                    token.content,
                    line_offsets,
                    token.map,
                )
                protected.extend(inline_code_spans)

        blocks.extend(self._blank_blocks(lines, line_offsets))
        blocks.sort(key=lambda block: (block.source_span.start, block.source_span.end))
        protected_sorted = self._merge_spans(protected)

        return MarkdownParseResult(
            blocks=tuple(blocks),
            protected_spans=protected_sorted,
        )

    @staticmethod
    def _token_map_to_char_span(
        map_lines: list[int] | None,
        line_offsets: list[int],
    ) -> SourceSpan | None:
        if map_lines is None or len(map_lines) != 2:
            return None

        return MarkdownParserAdapter._line_span_to_char_span(
            map_lines[0],
            map_lines[1],
            line_offsets,
        )

    @staticmethod
    def _line_span_to_char_span(
        line_start: int,
        line_end: int,
        line_offsets: list[int],
    ) -> SourceSpan:
        max_line = min(line_end, len(line_offsets) - 1)
        max_line = max(max_line, 0)

        start = line_offsets[max(line_start, 0)]
        end = line_offsets[max_line]
        if start > end:
            start, end = end, start

        return SourceSpan(start=start, end=end)

    @staticmethod
    def _line_offsets(lines: list[str]) -> list[int]:
        offsets: list[int] = [0]
        total = 0
        for line in lines:
            total += len(line)
            offsets.append(total)
        return offsets

    @staticmethod
    def _blank_blocks(lines: list[str], line_offsets: list[int]) -> list[MarkdownBlock]:
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
            span = MarkdownParserAdapter._line_span_to_char_span(
                blank_start,
                blank_end,
                line_offsets,
            )

            if span.start != span.end:
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

    @staticmethod
    def _extract_inline_code_spans(
        children: list[object],
        inline_markdown: str,
        line_offsets: list[int],
        inline_map: list[int] | None,
    ) -> list[SourceSpan]:
        if inline_map is None or len(inline_map) != 2:
            return []

        spans: list[SourceSpan] = []
        if len(children) == 0:
            return spans

        if inline_map[0] < 0:
            return spans

        search_offset = line_offsets[min(inline_map[0], len(line_offsets) - 1)]
        cursor = 0

        for child in children:
            if getattr(child, "type", None) != "code_inline":
                continue

            # markdown-it children usually do not expose char offsets by default,
            # fallback to inline regex matching for deterministic reconstruction.
            content = getattr(child, "content", "")
            marker = getattr(child, "markup", "`")
            pattern = re.escape(marker) + re.escape(content) + re.escape(marker)
            pattern_compiled = re.compile(pattern)
            match = pattern_compiled.search(inline_markdown, cursor)
            if match is None:
                continue

            spans.append(
                SourceSpan(
                    start=search_offset + match.start(),
                    end=search_offset + match.end(),
                )
            )
            cursor = match.end()

        if spans:
            return spans

        # Fallback: inline children may be omitted in older token builds.
        for match in _INLINE_CODE_PATTERN.finditer(inline_markdown):
            spans.append(
                SourceSpan(
                    start=search_offset + match.start(),
                    end=search_offset + match.end(),
                )
            )

        return spans

    @staticmethod
    def _merge_spans(spans: list[SourceSpan]) -> tuple[SourceSpan, ...]:
        if not spans:
            return ()

        ordered = sorted(spans, key=lambda item: (item.start, item.end))
        merged: list[SourceSpan] = [ordered[0]]

        for span in ordered[1:]:
            last = merged[-1]
            if span.start > last.end:
                merged.append(span)
            else:
                merged[-1] = SourceSpan(start=last.start, end=max(last.end, span.end))

        return tuple(merged)
