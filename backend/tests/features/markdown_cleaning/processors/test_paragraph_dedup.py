from app.features.markdown_cleaning.processors import (
    MarkdownBlockType,
    MarkdownParserAdapter,
    ParagraphDeduplicator,
)


def test_only_keeps_first_occurrence_of_duplicate_paragraphs() -> None:
    markdown = (
        "same paragraph\n\n"
        "same paragraph\n\n"
        "same paragraph\n\n"
        "tail paragraph.\n"
    )
    deduplicator = ParagraphDeduplicator()
    output, removed = deduplicator.deduplicate(markdown)

    assert removed == 2
    assert output.count("same paragraph") == 1
    assert output.count("tail paragraph.") == 1
    assert output == "same paragraph\n\ntail paragraph.\n"


def test_top_level_paragraphs_only() -> None:
    markdown = (
        "> q\n"
        "\n"
        "> q\n"
        "\n"
        "plain\n"
        "\n"
        "plain\n"
    )
    deduplicator = ParagraphDeduplicator()
    output, removed = deduplicator.deduplicate(markdown)

    assert removed == 1
    assert output == "> q\n\n> q\n\nplain\n\n"
    assert output.count("plain") == 1


def test_softbreak_and_whitespace_normalization_for_key() -> None:
    markdown = (
        "hello world\n"
        "next line\r\n"
        "\n"
        "hello    world\n"
        "next   line\n"
    )
    deduplicator = ParagraphDeduplicator()
    output, removed = deduplicator.deduplicate(markdown)

    assert removed == 1
    assert output.count("hello") == 1
    assert "hello world" in output


def test_case_punctuation_inline_markdown_remain_distinct() -> None:
    markdown = (
        "Hello [A](url) World.\n\n"
        "hello [A](url) world.\n\n"
        "Hello **A** World.\n\n"
        "Hello [A](url) World.\n"
    )
    deduplicator = ParagraphDeduplicator()
    output, removed = deduplicator.deduplicate(markdown)

    assert removed == 1
    assert output.count("Hello [A](url) World.") == 1
    assert output.count("hello [A](url) world.") == 1
    assert "Hello **A** World." in output


def test_noop_for_nested_list_blockquote_and_table_paragraphs() -> None:
    markdown = (
        "> nested quote\n"
        "\n"
        "> nested quote\n"
        "\n"
        "- item one\n"
        "\n"
        "- item one\n"
        "\n"
        "| h1 | h2 |\n"
        "| --- | --- |\n"
        "| c1 | c2 |\n"
        "\n"
        "| h1 | h2 |\n"
        "| --- | --- |\n"
        "| c1 | c2 |\n"
    )
    deduplicator = ParagraphDeduplicator()
    output, removed = deduplicator.deduplicate(markdown)

    assert removed == 0
    assert output == markdown


def test_non_paragraph_blocks_are_not_deduplicated() -> None:
    markdown = (
        "# heading\n\n"
        "# heading\n\n"
        "```text\n"
        "same text\n"
        "```\n"
        "```text\n"
        "same text\n"
        "```\n"
    )
    deduplicator = ParagraphDeduplicator()
    output, removed = deduplicator.deduplicate(markdown)

    assert removed == 0
    assert output == markdown


def test_second_run_keeps_no_more_duplicates() -> None:
    markdown = (
        "idempotent case.\n\n"
        "idempotent case.\n\n"
        "Stable text.\n"
    )
    deduplicator = ParagraphDeduplicator()
    first_output, first_removed = deduplicator.deduplicate(markdown)
    second_output, second_removed = deduplicator.deduplicate(first_output)

    assert first_removed == 1
    assert second_output == first_output
    assert second_removed == 0


def test_blockquote_starting_with_marker_keeps_following_plain() -> None:
    markdown = "> q\n\nplain\n"
    deduplicator = ParagraphDeduplicator()
    output, removed = deduplicator.deduplicate(markdown)

    assert removed == 0
    assert output == markdown


def test_list_item_prefix_keeps_following_plain() -> None:
    markdown = "- item\n\nplain\n"
    deduplicator = ParagraphDeduplicator()
    output, removed = deduplicator.deduplicate(markdown)

    assert removed == 0
    assert output == markdown


def test_byte_stable_when_no_dedup_candidates() -> None:
    markdown = (
        "# heading\n"
        "\n"
        "> note\n"
        "\n"
        "- item one\n"
        "\n"
        "- item one\n"
        "\n"
        "| h1 | h2 |\n"
        "| --- | --- |\n"
        "| a | b |\n"
    )
    deduplicator = ParagraphDeduplicator()
    output, removed = deduplicator.deduplicate(markdown)

    assert removed == 0
    assert output == markdown


def test_all_duplicates_at_end_keep_minimal_tail_newline() -> None:
    markdown = (
        "same\n\n"
        "same\n\n"
        "same\n"
    )
    deduplicator = ParagraphDeduplicator()
    output, removed = deduplicator.deduplicate(markdown)

    assert removed == 2
    assert output == "same\n\n"


def test_controlled_blank_lines_after_removal() -> None:
    markdown = (
        "first paragraph\n\n"
        "dup paragraph\n\n"
        "dup paragraph\n\n"
        "last paragraph\n"
    )
    deduplicator = ParagraphDeduplicator()
    output, removed = deduplicator.deduplicate(markdown)
    parser = MarkdownParserAdapter()

    original_blocks = parser.parse(output).blocks
    blank_runs = [
        block for block in original_blocks if block.block_type == MarkdownBlockType.BLANK
    ]

    assert removed == 1
    assert len(blank_runs) <= 3
    assert output == "first paragraph\n\ndup paragraph\n\nlast paragraph\n"
