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


def test_fence_and_html_blocks_are_protected() -> None:
    markdown = "```py\nprint(1)\n```\n"
    html_markdown = "<div>\nline\n</div>\n"

    result = MarkdownParserAdapter().parse(markdown + html_markdown)
    protected = result.protected_spans

    fenced = next(block for block in result.blocks if block.block_type == MarkdownBlockType.FENCED_CODE)
    html = next(block for block in result.blocks if block.block_type == MarkdownBlockType.HTML_BLOCK)

    assert fenced.source_span in protected
    assert html.source_span in protected
    assert fenced.source_span.start == 0
    assert fenced.source_span.end == len(markdown)
    assert html.source_span.start == len(markdown)


def test_inline_code_and_link_destination_are_protected() -> None:
    markdown = "before `alpha` and [ref](https://example.com/a) and `beta`\n"
    result = MarkdownParserAdapter().parse(markdown)

    spans = [(span.start, span.end) for span in result.protected_spans]
    assert (0, 0) not in spans
    assert (markdown.index("`alpha`"), markdown.index("`alpha`") + len("`alpha`")) in spans
    assert (markdown.index("`beta`"), markdown.index("`beta`") + len("`beta`")) in spans
    assert ("https://example.com/a" in markdown)
    assert (
        markdown.index("https://example.com/a"),
        markdown.index("https://example.com/a") + len("https://example.com/a"),
    ) in spans

    inline_types = [block.block_type for block in result.blocks]
    assert MarkdownBlockType.INLINE_CODE not in inline_types


def test_unclosed_fence_marked_invalid() -> None:
    markdown = "```py\nprint(1)\n"

    with pytest.raises(MarkdownParserError) as captured:
        MarkdownParserAdapter().parse(markdown)

    assert captured.value.code == MarkdownParserErrorCode.INVALID_MARKDOWN_INPUT


def test_table_block_is_parsed() -> None:
    markdown = "| h1 | h2 |\n| --- | --- |\n| a | b |\n"
    result = MarkdownParserAdapter().parse(markdown)
    assert MarkdownBlockType.TABLE in {block.block_type for block in result.blocks}


def test_duplicate_text_positions_and_span_stability() -> None:
    markdown = "`a` text `a` text\n```\ntext\n```\n`a`\n"
    result = MarkdownParserAdapter().parse(markdown)
    spans = list(result.protected_spans)

    assert spans == sorted(spans, key=lambda span: (span.start, span.end))
    for previous, current in zip(spans, spans[1:], strict=False):
        assert previous.end <= current.start

    assert (0, 3) in [(span.start, span.end) for span in spans]
    assert len([span for span in spans if span.end - span.start == 3]) >= 3
    assert (markdown.index("`a`", 6), markdown.index("`a`", 6) + 3) in [
        (span.start, span.end) for span in spans
    ]


def test_crlf_and_unicode_offsets_are_stable() -> None:
    markdown = "中文\n`a`\r\n> 引言\r\n- 项目\n"
    result = MarkdownParserAdapter().parse(markdown)

    assert MarkdownBlockType.HEADING not in {block.block_type for block in result.blocks}
    assert MarkdownBlockType.BLOCKQUOTE in {block.block_type for block in result.blocks}
    assert MarkdownBlockType.LIST_ITEM in {block.block_type for block in result.blocks}

    spans = list(result.protected_spans)
    assert (3, 6) in [(span.start, span.end) for span in spans]
    assert len(markdown) >= 3
