import pytest

from app.features.markdown_cleaning.processors.markdown_parser import (
    MarkdownBlockType,
    MarkdownInlineLeafType,
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

    leaf_kinds = [leaf.kind for leaf in result.inline_leaves]
    assert MarkdownInlineLeafType.CODE_INLINE in leaf_kinds
    assert MarkdownInlineLeafType.LINK_DESTINATION in leaf_kinds


def test_link_destination_supports_parenthesized_destination() -> None:
    markdown = "[x](foo(bar)) and [y](foo\\)bar)"
    result = MarkdownParserAdapter().parse(markdown)
    destinations = [
        markdown[leaf.source_span.start : leaf.source_span.end]
        for leaf in result.inline_leaves
        if leaf.kind == MarkdownInlineLeafType.LINK_DESTINATION
    ]

    assert "foo(bar)" in destinations
    assert "foo\\)bar" in destinations


def test_inline_code_double_delimiter_is_protected() -> None:
    markdown = "`` `a` `` and `b`\n"
    result = MarkdownParserAdapter().parse(markdown)

    code_leaf_spans = [
        (leaf.source_span.start, leaf.source_span.end)
        for leaf in result.inline_leaves
        if leaf.kind == MarkdownInlineLeafType.CODE_INLINE
    ]

    assert len(code_leaf_spans) >= 2
    assert (0, len("`` `a` ``")) in code_leaf_spans
    assert (markdown.index("`b`"), markdown.index("`b`") + 3) in code_leaf_spans


def test_inline_leaf_records_include_parent_block_kind() -> None:
    markdown = "[x](foo(bar)) <b>html</b> `` `a` ``\n"
    result = MarkdownParserAdapter().parse(markdown)
    blocks = {leaf.kind: leaf.parent_block_kind for leaf in result.inline_leaves}
    assert blocks[MarkdownInlineLeafType.LINK_DESTINATION] == MarkdownBlockType.PARAGRAPH
    assert blocks[MarkdownInlineLeafType.HTML_INLINE] == MarkdownBlockType.PARAGRAPH
    assert blocks[MarkdownInlineLeafType.CODE_INLINE] == MarkdownBlockType.PARAGRAPH


def test_html_inline_with_repeated_markup_stays_stable() -> None:
    markdown = "A <b>Hi</b> and <b>Hi</b>"
    result = MarkdownParserAdapter().parse(markdown)
    html_spans = [
        (leaf.source_span.start, leaf.source_span.end)
        for leaf in result.inline_leaves
        if leaf.kind == MarkdownInlineLeafType.HTML_INLINE
    ]

    expected = [
        (2, 5),
        (7, 11),
        (16, 19),
        (21, 25),
    ]
    assert html_spans == expected
    assert html_spans == sorted(html_spans)


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


def test_container_prefix_inline_leaves_use_absolute_spans() -> None:
    markdown = (
        "> [x](foo(bar))\n"
        "- item [y](foo\\)bar)\n"
        "# Heading > <b>Hi</b>\n"
        "- `` `a` ```\n"
        "> `` `b` ```\n"
    )
    result = MarkdownParserAdapter().parse(markdown)

    link_spans = [
        leaf.source_span
        for leaf in result.inline_leaves
        if leaf.kind == MarkdownInlineLeafType.LINK_DESTINATION
    ]
    link_texts = {
        markdown[span.start : span.end] for span in link_spans
    }

    assert link_texts == {"foo(bar)", "foo\\)bar"}

    assert (markdown.index("foo(bar)"), markdown.index("foo(bar)") + len("foo(bar)")) in [
        (span.start, span.end) for span in link_spans
    ]
    assert (markdown.index("foo\\)bar"), markdown.index("foo\\)bar") + len("foo\\)bar")) in [
        (span.start, span.end) for span in link_spans
    ]

    parent_by_text = {
        markdown[leaf.source_span.start : leaf.source_span.end]: leaf.parent_block_kind
        for leaf in result.inline_leaves
        if leaf.kind in {
            MarkdownInlineLeafType.LINK_DESTINATION,
            MarkdownInlineLeafType.CODE_INLINE,
            MarkdownInlineLeafType.HTML_INLINE,
        }
    }
    assert parent_by_text["foo(bar)"] == MarkdownBlockType.BLOCKQUOTE
    assert parent_by_text["foo\\)bar"] == MarkdownBlockType.LIST_ITEM
    assert parent_by_text["<b>"] == MarkdownBlockType.HEADING


def test_container_blockquote_and_list_code_inline_spans_are_absolute() -> None:
    markdown = (
        "- `` `a` ```\n"
        "> `` `b` ```\n"
    )
    result = MarkdownParserAdapter().parse(markdown)

    code_spans = {
        (leaf.source_span.start, leaf.source_span.end)
        for leaf in result.inline_leaves
        if leaf.kind == MarkdownInlineLeafType.CODE_INLINE
    }

    assert (markdown.index("`a`"), markdown.index("`a`") + 3) in code_spans
    assert (markdown.index("`b`"), markdown.index("`b`") + 3) in code_spans
