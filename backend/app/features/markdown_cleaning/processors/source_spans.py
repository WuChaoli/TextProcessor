from collections.abc import Iterable
from re import Pattern, compile

from app.features.markdown_cleaning.processors.models import SourceSpan

_PARENTHESIZED_LINK_PATTERN: Pattern[str] = compile(r"\(")


def build_line_offsets(markdown: str) -> tuple[int, ...]:
    offsets: list[int] = [0]
    total = 0
    for chunk in markdown.splitlines(keepends=True):
        total += len(chunk)
        offsets.append(total)
    return tuple(offsets)


def source_span_from_line_map(
    line_map: list[int] | None,
    line_offsets: tuple[int, ...],
) -> SourceSpan | None:
    if line_map is None or len(line_map) != 2:
        return None

    line_start, line_end = line_map
    if line_offsets is None or len(line_offsets) < 2:
        return None

    line_start = max(0, line_start)
    line_end = min(len(line_offsets) - 1, line_end)
    if line_start > line_end:
        return None

    start = line_offsets[line_start]
    end = line_offsets[line_end]
    if end < start:
        return None
    return SourceSpan(start=start, end=end)


def ensure_parenthesized_span(
    markdown: str,
    anchor: int,
    destination: str,
) -> SourceSpan | None:
    if anchor < 0:
        return None

    open_index = _PARENTHESIZED_LINK_PATTERN.search(markdown, anchor)
    if open_index is None:
        return None

    open_pos = open_index.end()
    if open_pos >= len(markdown):
        return None

    if markdown[open_pos] == "<":
        open_pos += 1
        close_pos = markdown.find(">", open_pos)
        if close_pos < 0:
            return None
        start = open_pos
        end = close_pos
        if markdown[close_pos + 1 : close_pos + 2] == ")":
            return SourceSpan(start=start, end=end)
        return None

    if not markdown.startswith(destination, open_pos):
        # backtrack from anchor to locate destination with potential escaped parentheses.
        found = markdown.find(destination, open_pos)
        if found < 0:
            return None
        open_pos = found

    start = open_pos
    end = open_pos + len(destination)
    if end < len(markdown) and markdown[end] == ")":
        return SourceSpan(start=start, end=end)
    if end < len(markdown) and markdown[end].isspace():
        # tolerate title following destination, e.g. (url "title")
        close_pos = markdown.find(")", end)
        if close_pos > end:
            return SourceSpan(start=start, end=end)
    return SourceSpan(start=start, end=end)


def merge_source_spans(spans: Iterable[SourceSpan]) -> tuple[SourceSpan, ...]:
    ordered: list[SourceSpan] = []
    for span in spans:
        if span.start < 0 or span.end < span.start:
            continue
        ordered.append(span)

    if not ordered:
        return ()

    ordered.sort(key=lambda item: (item.start, item.end))
    merged: list[SourceSpan] = [ordered[0]]
    for span in ordered[1:]:
        prev = merged[-1]
        if span.start < prev.end:
            merged[-1] = SourceSpan(start=prev.start, end=max(prev.end, span.end))
        else:
            merged.append(span)
    return tuple(merged)
