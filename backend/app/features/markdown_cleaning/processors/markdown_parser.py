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


class MarkdownInlineLeafType(StrEnum):
    CODE_INLINE = "code_inline"
    HTML_INLINE = "html_inline"
    LINK_DESTINATION = "link_destination"
    IMAGE_DESTINATION = "image_destination"


@dataclass(frozen=True, slots=True)
class MarkdownBlock:
    block_type: MarkdownBlockType
    line_start: int
    line_end: int
    source_span: SourceSpan


@dataclass(frozen=True, slots=True)
class MarkdownInlineLeaf:
    kind: MarkdownInlineLeafType
    parent_block_kind: MarkdownBlockType
    source_span: SourceSpan


@dataclass(frozen=True, slots=True)
class MarkdownParseResult:
    blocks: tuple[MarkdownBlock, ...]
    protected_spans: tuple[SourceSpan, ...]
    inline_leaves: tuple[MarkdownInlineLeaf, ...]


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


class MarkdownParserAdapter:
    def __init__(self, markdown_it: MarkdownIt | None = None) -> None:
        self._markdown_it = markdown_it or MarkdownIt("commonmark").enable("table")

    def parse_file(self, source: Path) -> MarkdownParseResult:
        markdown = source.read_text(encoding="utf-8")
        return self.parse(markdown)

    def parse(self, markdown: str) -> MarkdownParseResult:
        self._validate_fenced_code_blocks_closed(markdown)

        try:
            environment: dict[str, object] = {}
            tokens = self._markdown_it.parse(markdown, environment)
        except Exception as exc:
            raise MarkdownParserError(
                MarkdownParserErrorCode.MARKDOWN_PARSE_FAILED,
                "Markdown parser execution failed",
            ) from exc

        line_offsets = build_line_offsets(markdown)
        lines = markdown.splitlines(keepends=True)

        blocks: list[MarkdownBlock] = []
        protected: list[SourceSpan] = []
        inline_leaves: list[MarkdownInlineLeaf] = []
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
                        line_end = inline_map[1]
                    except TypeError as exc:
                        raise MarkdownParserError(
                            MarkdownParserErrorCode.MARKDOWN_PARSE_FAILED,
                            "inline token line map is invalid",
                        ) from exc

                    inline_content, inline_offsets = self._build_inline_source_projection(
                        markdown=markdown,
                        line_offsets=line_offsets,
                        inline_map=[line_start, line_end],
                        open_stack=open_stack,
                        expected_content=token.content,
                    )
                    if not inline_content:
                        continue
                    parent_block_kind = self._current_parent_block_kind(open_stack)
                    inline_leaves.extend(
                        self._collect_inline_leaves(
                            token.children,
                            inline_content,
                            inline_offsets,
                            parent_block_kind,
                        )
                    )

            inline_leaves.extend(
                self._collect_reference_definition_leaves(
                    markdown,
                    line_offsets,
                    environment.get("references"),
                    protected,
                )
            )
            inline_leaves.sort(
                key=lambda leaf: (leaf.source_span.start, leaf.source_span.end)
            )
            protected.extend(leaf.source_span for leaf in inline_leaves)

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

        return MarkdownParseResult(
            blocks=tuple(blocks),
            protected_spans=protected_sorted,
            inline_leaves=tuple(inline_leaves),
        )

    @staticmethod
    def _current_parent_block_kind(
        open_stack: list[tuple[MarkdownBlockType, int, int]],
    ) -> MarkdownBlockType:
        if not open_stack:
            return MarkdownBlockType.PARAGRAPH

        for block_type, _, _ in reversed(open_stack):
            if block_type != MarkdownBlockType.PARAGRAPH:
                return block_type

        return open_stack[-1][0]

    @staticmethod
    def _build_inline_source_projection(
        markdown: str,
        line_offsets: tuple[int, ...],
        inline_map: list[int] | tuple[int, int],
        open_stack: list[tuple[MarkdownBlockType, int, int]],
        expected_content: str = "",
    ) -> tuple[str, tuple[int, ...]]:
        if len(inline_map) != 2:
            return "", ()

        line_start = inline_map[0]
        line_end = inline_map[1]
        if line_start < 0 or line_end < line_start:
            return "", ()

        try:
            raw_start = line_offsets[line_start]
            raw_end = line_offsets[line_end]
        except (TypeError, IndexError):
            return "", ()

        raw_block = markdown[raw_start:raw_end]
        if not raw_block:
            return "", ()

        open_blocks = [block_type for block_type, _, _ in open_stack]
        blockquote_depth = open_blocks.count(MarkdownBlockType.BLOCKQUOTE)
        list_depth = open_blocks.count(MarkdownBlockType.LIST_ITEM)
        is_heading = MarkdownBlockType.HEADING in open_blocks

        lines = raw_block.splitlines(keepends=True)
        if not lines:
            return "", ()

        list_indents: list[int] = []
        if list_depth > 0 and lines:
            first_cursor = 0
            first_line = lines[0]
            first_cursor = MarkdownParserAdapter._consume_blockquote_prefix(
                first_line,
                first_cursor,
                blockquote_depth,
            )
            if is_heading:
                first_cursor = MarkdownParserAdapter._consume_heading_prefix(
                    first_line,
                    first_cursor,
                )
            for _ in range(list_depth):
                first_cursor, indent = MarkdownParserAdapter._consume_list_prefix(
                    first_line,
                    first_cursor,
                )
                if indent == 0:
                    break
                list_indents.append(indent)

        mapped_chars: list[str] = []
        offset_map: list[int] = []
        line_cursor = raw_start

        for line_index, line in enumerate(lines):
            cursor = 0
            cursor = MarkdownParserAdapter._consume_blockquote_prefix(
                line,
                cursor,
                blockquote_depth,
            )
            if is_heading and line_index == 0:
                cursor = MarkdownParserAdapter._consume_heading_prefix(line, cursor)

            if list_indents:
                if line_index == 0:
                    temp_cursor = cursor
                    for _ in range(list_depth):
                        temp_cursor, consumed = MarkdownParserAdapter._consume_list_prefix(
                            line,
                            temp_cursor,
                        )
                        if consumed == 0:
                            break
                        cursor = temp_cursor
                else:
                    cursor = MarkdownParserAdapter._consume_list_indent_prefix(
                        line,
                        cursor,
                        list_indents,
                    )

            absolute_position = line_cursor + cursor
            while cursor < len(line):
                if (
                    line[cursor] == "\r"
                    and cursor + 1 < len(line)
                    and line[cursor + 1] == "\n"
                ):
                    mapped_chars.append("\n")
                    offset_map.append(absolute_position)
                    cursor += 2
                    absolute_position += 2
                    continue

                mapped_chars.append(line[cursor])
                offset_map.append(absolute_position)
                cursor += 1
                absolute_position += 1
            line_cursor += len(line)

        mapped = "".join(mapped_chars)
        expected = expected_content.replace("\r\n", "\n")

        if mapped != expected:
            trailing_chars = mapped[len(expected) :]
            if mapped.startswith(expected) and trailing_chars and trailing_chars.strip("\n") == "":
                mapped = expected
                offset_map = offset_map[: len(expected)]
            else:
                return "", ()

        if len(mapped) != len(offset_map):
            return "", ()

        if "".join(mapped) != expected:
            return "", ()

        return mapped, tuple(offset_map)

    @staticmethod
    def _consume_list_indent_prefix(
        line: str,
        cursor: int,
        list_indents: list[int],
    ) -> int:
        for indent in list_indents:
            next_cursor = cursor
            removed = 0
            while removed < indent and next_cursor < len(line):
                if line[next_cursor] not in " \t":
                    break
                next_cursor += 1
                removed += 1
            cursor = next_cursor
        return cursor

    @staticmethod
    def _consume_list_prefix(line: str, cursor: int) -> tuple[int, int]:
        n = len(line)
        start = cursor
        while cursor < n and line[cursor] in " \t":
            cursor += 1
        marker_start = cursor
        if cursor >= n:
            return start, 0

        first = line[cursor]
        if first in ("-", "+", "*"):
            cursor += 1
            if cursor < n and line[cursor] in "\t ":
                while cursor < n and line[cursor] in "\t ":
                    cursor += 1
                return cursor, cursor - start
            return start, 0

        if not first.isdigit():
            return start, 0

        while cursor < n and line[cursor].isdigit():
            cursor += 1
        if cursor >= n:
            return start, 0

        if line[cursor] not in (".", ")"):
            return start, 0

        cursor += 1
        if cursor >= n or line[cursor] not in "\t ":
            return start, 0

        while cursor < n and line[cursor] in "\t ":
            cursor += 1

        return cursor, cursor - marker_start

    @staticmethod
    def _consume_blockquote_prefix(
        line: str,
        cursor: int,
        depth: int,
    ) -> int:
        n = len(line)
        for _ in range(depth):
            while cursor < n and line[cursor] in " \t":
                cursor += 1
            if cursor >= n or line[cursor] != ">":
                break
            cursor += 1
            while cursor < n and line[cursor] in " \t":
                cursor += 1
        return cursor

    @staticmethod
    def _consume_heading_prefix(line: str, cursor: int) -> int:
        n = len(line)
        if cursor >= n:
            return cursor
        start = cursor
        # heading in markdown is up to 6 # followed by at least one whitespace
        while cursor < n and line[cursor] in " \t":
            cursor += 1
        marker = 0
        while cursor < n and line[cursor] == "#" and marker < 6:
            cursor += 1
            marker += 1
        if marker == 0:
            return start
        if cursor >= n or line[cursor] not in "\t ":
            return start
        while cursor < n and line[cursor] in "\t ":
            cursor += 1
        return cursor

    def _collect_inline_leaves(
        self,
        children: Sequence[object],
        inline_markdown: str,
        inline_offsets: tuple[int, ...],
        parent_block_kind: MarkdownBlockType,
    ) -> list[MarkdownInlineLeaf]:
        leaves: list[MarkdownInlineLeaf] = []
        cursor = 0

        for child in children:
            child_type = getattr(child, "type", None)
            if child_type == "code_inline":
                span = self._extract_code_inline_span(
                    child,
                    inline_markdown,
                    cursor,
                )
                if span is None:
                    continue
                source_span = self._rebase_span(span, inline_offsets)
                if source_span is None:
                    continue
                leaves.append(
                    MarkdownInlineLeaf(
                        kind=MarkdownInlineLeafType.CODE_INLINE,
                        parent_block_kind=parent_block_kind,
                        source_span=source_span,
                    )
                )
                cursor = span[1]
                continue

            if child_type == "html_inline":
                content = getattr(child, "content", "")
                if not isinstance(content, str):
                    continue
                span = self._find_lexeme_span(inline_markdown, cursor, content)
                if span is not None:
                    source_span = self._rebase_span(span, inline_offsets)
                    if source_span is None:
                        continue
                    leaves.append(
                        MarkdownInlineLeaf(
                            kind=MarkdownInlineLeafType.HTML_INLINE,
                            parent_block_kind=parent_block_kind,
                            source_span=source_span,
                        )
                    )
                    cursor = span[1]
                continue

            if child_type == "link_open":
                destination = self._get_child_attribute(child, "href")
                if destination:
                    markup = getattr(child, "markup", "")
                    span = (
                        self._extract_autolink_destination_span(
                            inline_markdown,
                            cursor,
                            destination,
                        )
                        if markup == "autolink"
                        else self._extract_destination_span(
                            inline_markdown,
                            cursor,
                            destination,
                        )
                    )
                    if span is not None:
                        source_span = self._rebase_span(span, inline_offsets)
                        if source_span is None:
                            continue
                        leaves.append(
                            MarkdownInlineLeaf(
                                kind=MarkdownInlineLeafType.LINK_DESTINATION,
                                parent_block_kind=parent_block_kind,
                                source_span=source_span,
                            )
                        )
                        cursor = span[1]
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
                        source_span = self._rebase_span(span, inline_offsets)
                        if source_span is None:
                            continue
                        leaves.append(
                            MarkdownInlineLeaf(
                                kind=MarkdownInlineLeafType.IMAGE_DESTINATION,
                                parent_block_kind=parent_block_kind,
                                source_span=source_span,
                            )
                        )
                        cursor = span[1]
                continue

        return leaves

    @staticmethod
    def _extract_autolink_destination_span(
        inline_markdown: str,
        cursor: int,
        destination: str,
    ) -> tuple[int, int] | None:
        anchor = cursor
        while anchor < len(inline_markdown):
            start_marker = inline_markdown.find("<", anchor)
            if start_marker < 0:
                return None
            end_marker = inline_markdown.find(">", start_marker + 1)
            if end_marker < 0:
                return None
            start = start_marker + 1
            raw_destination = inline_markdown[start:end_marker]
            normalized = _normalize_destination(raw_destination)
            if normalized == destination or f"mailto:{normalized}" == destination:
                return (start, end_marker)
            anchor = start_marker + 1
        return None

    @classmethod
    def _collect_reference_definition_leaves(
        cls,
        markdown: str,
        line_offsets: tuple[int, ...],
        references: object,
        existing_protected: Sequence[SourceSpan],
    ) -> list[MarkdownInlineLeaf]:
        if not isinstance(references, dict):
            return []

        leaves: list[MarkdownInlineLeaf] = []
        seen: set[tuple[int, int]] = set()
        for reference in references.values():
            if not isinstance(reference, dict):
                continue
            href = reference.get("href")
            line_map = reference.get("map")
            if (
                not isinstance(href, str)
                or not isinstance(line_map, list)
                or len(line_map) != 2
                or not isinstance(line_map[0], int)
                or not isinstance(line_map[1], int)
            ):
                continue
            typed_line_map = [line_map[0], line_map[1]]
            definition_span = source_span_from_line_map(
                typed_line_map,
                line_offsets,
            )
            if definition_span is None:
                continue
            definition = markdown[definition_span.start : definition_span.end]
            span = cls._extract_reference_destination_span(definition, href)
            if span is None:
                continue
            source_span = SourceSpan(
                start=definition_span.start + span[0],
                end=definition_span.start + span[1],
            )
            span_key = (source_span.start, source_span.end)
            if span_key in seen or any(
                protected.start <= source_span.start
                and source_span.end <= protected.end
                for protected in existing_protected
            ):
                continue
            seen.add(span_key)
            leaves.append(
                MarkdownInlineLeaf(
                    kind=MarkdownInlineLeafType.LINK_DESTINATION,
                    parent_block_kind=MarkdownBlockType.PARAGRAPH,
                    source_span=source_span,
                )
            )
        return leaves

    @classmethod
    def _extract_reference_destination_span(
        cls,
        definition: str,
        destination: str,
    ) -> tuple[int, int] | None:
        delimiter = definition.find("]:")
        if delimiter < 0:
            return None
        start = delimiter + 2
        while start < len(definition) and definition[start].isspace():
            start += 1
        if start >= len(definition):
            return None

        if definition[start] == "<":
            end_marker = definition.find(">", start + 1)
            if end_marker < 0:
                return None
            raw_destination = definition[start + 1 : end_marker]
            if _normalize_destination(raw_destination) == destination:
                return (start + 1, end_marker)
            return None

        end = cls._scan_reference_destination_end(definition, start)
        if end is None:
            return None
        if _normalize_destination(definition[start:end]) != destination:
            return None
        return (start, end)

    @staticmethod
    def _scan_reference_destination_end(definition: str, start: int) -> int | None:
        depth = 0
        offset = start
        while offset < len(definition):
            char = definition[offset]
            if char == "\\":
                if offset + 1 >= len(definition):
                    return None
                offset += 2
                continue
            if char == "(":
                depth += 1
            elif char == ")":
                if depth == 0:
                    break
                depth -= 1
            elif char.isspace() and depth == 0:
                break
            offset += 1
        if depth != 0 or offset == start:
            return None
        return offset

    @staticmethod
    def _find_lexeme_span(
        inline_markdown: str,
        cursor: int,
        content: str,
    ) -> tuple[int, int] | None:
        if not content:
            return None

        index = inline_markdown.find(content, cursor)
        if index < 0:
            return None
        return (index, index + len(content))

    def _extract_code_inline_span(
        self,
        child: object,
        inline_markdown: str,
        cursor: int,
    ) -> tuple[int, int] | None:
        markup = getattr(child, "markup", "")
        content = getattr(child, "content", "")
        if not isinstance(markup, str) or not isinstance(content, str) or not markup:
            return None

        span = self._extract_token_span(inline_markdown, cursor, markup, content)
        if span is None:
            span = self._extract_code_span_by_delimiter_run(
                inline_markdown,
                cursor,
                markup,
                content,
            )

        if span is None:
            return None
        return span

    @staticmethod
    def _extract_token_span(
        inline_markdown: str,
        cursor: int,
        prefix: str,
        content: str,
    ) -> tuple[int, int] | None:
        match = re.compile(
            rf"{re.escape(prefix)}{re.escape(content)}{re.escape(prefix)}",
        ).search(inline_markdown, cursor)
        if match is None:
            return None
        return (match.start(), match.end())

    @staticmethod
    def _extract_code_span_by_delimiter_run(
        inline_markdown: str,
        cursor: int,
        delimiter: str,
        content: str,
    ) -> tuple[int, int] | None:
        open_cursor = cursor
        delimiter_length = len(delimiter)

        while True:
            open_index = inline_markdown.find(delimiter, open_cursor)
            if open_index < 0:
                return None

            search_from = open_index + delimiter_length
            while True:
                close_index = inline_markdown.find(delimiter, search_from)
                if close_index < 0:
                    break

                candidate_body = inline_markdown[
                    open_index + delimiter_length : close_index
                ]
                if _normalize_code_body(candidate_body) == content:
                    return (open_index, close_index + delimiter_length)

                search_from = close_index + delimiter_length

            open_cursor = open_index + delimiter_length

    @staticmethod
    def _extract_destination_span(
        inline_markdown: str,
        cursor: int,
        destination: str,
    ) -> tuple[int, int] | None:
        anchor = cursor
        while anchor < len(inline_markdown):
            link_start = inline_markdown.find("](", anchor)
            if link_start < 0:
                return None

            open_paren = link_start + 2
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
                if inline_markdown[tail : tail + 1] == ")":
                    if _normalize_destination(inline_markdown[start:end]) == destination:
                        return (start, end)
                if _normalize_destination(inline_markdown[start:end]) == destination:
                    return (start, end)
            else:
                start = open_paren
                destination_end = MarkdownParserAdapter._scan_destination_end(
                    inline_markdown,
                    start,
                )
                if destination_end is None:
                    anchor = link_start + 1
                    continue

                raw_destination = inline_markdown[start:destination_end]
                if _normalize_destination(raw_destination) == destination:
                    return (start, destination_end)

            anchor = link_start + 1

        return None

    @staticmethod
    def _rebase_span(
        span: tuple[int, int],
        inline_offsets: tuple[int, ...],
    ) -> SourceSpan | None:
        start, end = span
        if start < 0 or end < start or end > len(inline_offsets):
            return None
        if start == end:
            if start == len(inline_offsets):
                return SourceSpan(start=inline_offsets[-1], end=inline_offsets[-1])
            return SourceSpan(start=inline_offsets[start], end=inline_offsets[start])
        if end > len(inline_offsets) or start >= len(inline_offsets):
            return None
        start_offset = inline_offsets[start]
        end_offset = inline_offsets[end - 1] + 1
        if end_offset < start_offset:
            return None
        return SourceSpan(start=start_offset, end=end_offset)

    @staticmethod
    def _scan_destination_end(inline_markdown: str, start: int) -> int | None:
        depth = 0
        offset = start

        while offset < len(inline_markdown):
            char = inline_markdown[offset]
            if char == "\\":
                if offset + 1 < len(inline_markdown):
                    offset += 2
                    continue
                return None

            if char == "(":
                depth += 1
            elif char == ")":
                if depth == 0:
                    return offset
                depth -= 1
            elif char in ("\n", "\r"):
                return None
            elif depth == 0 and char.isspace():
                return offset

            offset += 1

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

            span = source_span_from_line_map([blank_start, blank_end], line_offsets)
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


def _normalize_destination(destination: str) -> str:
    normalized: list[str] = []
    cursor = 0
    while cursor < len(destination):
        char = destination[cursor]
        if char == "\\" and cursor + 1 < len(destination):
            cursor += 1
            normalized.append(destination[cursor])
            cursor += 1
            continue

        normalized.append(char)
        cursor += 1
    return "".join(normalized)


def _normalize_code_body(body: str) -> str:
    if len(body) >= 2 and body[0] == " " and body[-1] == " ":
        return body[1:-1]
    return body
