from pathlib import Path

import pytest

from app.features.markdown_cleaning.processors.markdown_parser import (
    MarkdownBlockType,
    MarkdownParserAdapter,
    MarkdownParserError,
    MarkdownParserErrorCode,
)


def test_parse_blocks_include_expected_types() -> None:
    markdown = (
        "# Title\n\n"
        "paragraph one.\n\n"
        "```py\n"
        "x = 1\n"
        "```\n\n"
        "> quote\n\n"
        "- item one\n"
    )

    result = MarkdownParserAdapter().parse(markdown)
    block_types = [block.block_type for block in result.blocks]

    assert MarkdownBlockType.HEADING in block_types
    assert MarkdownBlockType.PARAGRAPH in block_types
    assert MarkdownBlockType.FENCED_CODE in block_types
    assert MarkdownBlockType.BLOCKQUOTE in block_types
    assert MarkdownBlockType.LIST_ITEM in block_types
    assert MarkdownBlockType.BLANK in block_types


def test_fence_open_and_close_are_recorded_as_protected() -> None:
    markdown = "```py\nprint(1)\n```\n"

    result = MarkdownParserAdapter().parse(markdown)
    protected = result.protected_spans

    fenced_blocks = [block for block in result.blocks if block.block_type == MarkdownBlockType.FENCED_CODE]
    assert len(fenced_blocks) == 1
    fenced = fenced_blocks[0].source_span

    assert fenced == protected[0]
    assert fenced.start == 0
    assert fenced.end == len(markdown)


def test_html_block_is_protected() -> None:
    markdown = "<div>\nline\n</div>\n\nafter\n"

    result = MarkdownParserAdapter().parse(markdown)
    blocks = [block for block in result.blocks if block.block_type == MarkdownBlockType.HTML_BLOCK]
    assert len(blocks) == 1
    assert blocks[0].source_span.start == 0
    assert blocks[0].source_span.end == len("<div>\nline\n</div>\n")
    assert any(
        blocks[0].source_span.start == span.start and blocks[0].source_span.end == span.end
        for span in result.protected_spans
    )


def test_inline_code_is_recorded_as_protected() -> None:
    markdown = "before `alpha` after\n"
    result = MarkdownParserAdapter().parse(markdown)

    inline_spans = [
        block.source_span
        for block in result.blocks
        if block.block_type == MarkdownBlockType.INLINE_CODE
    ]
    assert inline_spans == []

    expected = (markdown.index("`alpha`"), markdown.index("`alpha`") + len("`alpha`"))
    assert (expected[0], expected[1]) in [
        (span.start, span.end) for span in result.protected_spans
    ]


def test_unclosed_fence_marked_invalid() -> None:
    markdown = "```py\nprint(1)\n"

    with pytest.raises(MarkdownParserError) as captured:
        MarkdownParserAdapter().parse(markdown)

    assert captured.value.code == MarkdownParserErrorCode.INVALID_MARKDOWN_INPUT


def test_table_block_is_parsed() -> None:
    markdown = "| h1 | h2 |\n| --- | --- |\n| a | b |\n"
    result = MarkdownParserAdapter().parse(markdown)

    block_types = [block.block_type for block in result.blocks]
    assert MarkdownBlockType.TABLE in block_types


def test_protected_spans_are_sorted_and_non_overlapping() -> None:
    markdown = "`a` text\n```\ntext\n```\n"
    result = MarkdownParserAdapter().parse(markdown)

    spans = result.protected_spans
    assert list(spans) == sorted(spans, key=lambda span: (span.start, span.end))
    for previous, current in zip(spans, spans[1:]):
        assert previous.end <= current.start
