import pytest

import app.features.markdown_cleaning.processors.markdown_formatter as markdown_formatter
from app.features.markdown_cleaning.processors import MarkdownParserAdapter
from app.features.markdown_cleaning.processors.errors import (
    MarkdownCleaningErrorCode,
    MarkdownCleaningProcessorError,
)
from app.features.markdown_cleaning.processors.markdown_formatter import (
    MarkdownFormatterAdapter,
)
from app.features.markdown_cleaning.processors.markdown_parser import (
    MarkdownBlockType,
)


def _collect_visible_signature(markdown: str) -> str:
    parsed = MarkdownParserAdapter().parse(markdown)
    return MarkdownFormatterAdapter._collect_visible_semantic_signature(markdown, parsed)


def _collect_protected_units(markdown: str) -> tuple[tuple[str, ...], ...]:
    parsed = MarkdownParserAdapter().parse(markdown)
    return MarkdownFormatterAdapter._collect_protected_units(markdown, parsed)


def _collect_block_signature(markdown: str) -> tuple[MarkdownBlockType, ...]:
    parsed = MarkdownParserAdapter().parse(markdown)
    return tuple(
        block.block_type
        for block in parsed.blocks
        if block.block_type is not MarkdownBlockType.BLANK
    )


def _collect_inline_signature(markdown: str) -> tuple[tuple[str, str], ...]:
    parsed = MarkdownParserAdapter().parse(markdown)
    return tuple(
        (leaf.kind.value, leaf.parent_block_kind.value)
        for leaf in parsed.inline_leaves
    )


def test_markdown_formatter_checks_ast_semantics_and_links_and_protection() -> None:
    markdown = (
        "# Title  \r\n"
        "  -  item one  \r\n"
        ".\r\n"
        "> blockquote  with   spaces\r\n"
        "\r\n"
        "|h1|h2|\r\n"
        "|---|---|\r\n"
        "| A | B |\r\n"
        "\r\n"
        "[ref](https://example.com/path?q=1#t) and [img](https://example.com/img)\r\n"
        "\r\n"
        "```py\r\n"
        "print('x')\r\n"
        "```\r\n"
        "<span>raw</span>\r\n"
        "`inline`\r\n"
    )

    formatter = MarkdownFormatterAdapter()
    result = formatter.format(markdown)

    assert result.formatting_changes >= 1
    assert not result.text.startswith("\ufeff")
    assert "\r" not in result.text
    assert result.text.endswith("\n")
    assert not result.text.endswith("\n\n")
    assert _collect_block_signature(markdown) == _collect_block_signature(result.text)
    assert _collect_inline_signature(markdown) == _collect_inline_signature(result.text)
    assert (
        _collect_visible_signature(markdown)
        == _collect_visible_signature(result.text)
    )
    assert _collect_protected_units(markdown) == _collect_protected_units(result.text)


def test_formatting_second_pass_is_byte_stable() -> None:
    markdown = "##  Title  \n\n-   item one  \n\n```text\nx = 1\n```\n"
    formatter = MarkdownFormatterAdapter()
    first = formatter.format(markdown)
    second = formatter.format(first.text)

    assert first.text == second.text
    assert second.formatting_changes == 0


def test_non_protected_plaintext_change_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    markdown = "hello `x` world\n"

    def _bad_format(*_args: object, **_kwargs: object) -> str:
        return "goodbye `x` world\n"

    monkeypatch.setattr(markdown_formatter.mdformat, "text", _bad_format)
    with pytest.raises(MarkdownCleaningProcessorError) as exc:
        MarkdownFormatterAdapter().format(markdown)

    assert (
        exc.value.code is MarkdownCleaningErrorCode.MARKDOWN_NORMALIZATION_FAILED
    )


def test_protected_reorder_or_duplicate_violation_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    markdown = "a `x` b `x` c\n"

    def _bad_format(*_args: object, **_kwargs: object) -> str:
        return "a `x` `x` b c\n"

    monkeypatch.setattr(markdown_formatter.mdformat, "text", _bad_format)
    with pytest.raises(MarkdownCleaningProcessorError) as exc:
        MarkdownFormatterAdapter().format(markdown)

    assert (
        exc.value.code is MarkdownCleaningErrorCode.MARKDOWN_NORMALIZATION_FAILED
    )


def test_non_table_pipe_change_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    markdown = "a|b\n"

    def _bad_format(*_args: object, **_kwargs: object) -> str:
        return "a | b\n"

    monkeypatch.setattr(markdown_formatter.mdformat, "text", _bad_format)
    with pytest.raises(MarkdownCleaningProcessorError) as exc:
        MarkdownFormatterAdapter().format(markdown)

    assert (
        exc.value.code is MarkdownCleaningErrorCode.MARKDOWN_NORMALIZATION_FAILED
    )


def test_table_pipe_normalization_only_in_table(monkeypatch: pytest.MonkeyPatch) -> None:
    markdown = "|a|b|\n|---|---|\n|x|y|\n"

    def _bad_format(*_args: object, **_kwargs: object) -> str:
        return "| a | b |\n| --- | --- |\n| x | y |\n"

    monkeypatch.setattr(markdown_formatter.mdformat, "text", _bad_format)
    result = MarkdownFormatterAdapter().format(markdown)

    assert result.formatting_changes >= 1
    assert _collect_visible_signature(markdown) == _collect_visible_signature(result.text)


def test_table_pipe_normalization_with_link_prefix_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    markdown = "[link](https://example.com)\n|a|b|\n|---|---|\n|x|y|\n"

    def _bad_format(*_args: object, **_kwargs: object) -> str:
        return "[link](https://example.com)\n| a | b |\n| --- | --- |\n| x | y |\n"

    monkeypatch.setattr(markdown_formatter.mdformat, "text", _bad_format)
    result = MarkdownFormatterAdapter().format(markdown)

    assert result.formatting_changes >= 1
    assert _collect_visible_signature(markdown) == _collect_visible_signature(result.text)


def test_table_pipe_change_with_code_prefix_requires_no_semantic_delta(monkeypatch: pytest.MonkeyPatch) -> None:
    markdown = "`code`\n|a|b|\n|---|---|\n|x|y|\n"

    def _bad_format(*_args: object, **_kwargs: object) -> str:
        return "`code`\n| a | b |\n| --- | --- |\n| x | y |\n"

    monkeypatch.setattr(markdown_formatter.mdformat, "text", _bad_format)
    result = MarkdownFormatterAdapter().format(markdown)

    assert result.formatting_changes >= 1
    assert _collect_visible_signature(markdown) == _collect_visible_signature(result.text)


@pytest.mark.parametrize(
    ("markdown", "formatted"),
    [
        (
            "[link](https://example.com)\n|a|b|\n|---|---|\n|x|y|\n",
            "[link](https://example.com)\n| x | b |\n| --- | --- |\n| x | y |\n",
        ),
        (
            "`code`\n|a|b|\n|---|---|\n|x|y|\n",
            "`code`\n| a | b |\n| --- | --- |\n| z | y |\n",
        ),
    ],
)
def test_table_pipe_variation_and_semantic_change_is_rejected(
    markdown: str,
    formatted: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _bad_format(*_args: object, **_kwargs: object) -> str:
        return formatted

    monkeypatch.setattr(markdown_formatter.mdformat, "text", _bad_format)
    with pytest.raises(MarkdownCleaningProcessorError) as exc:
        MarkdownFormatterAdapter().format(markdown)

    assert (
        exc.value.code is MarkdownCleaningErrorCode.MARKDOWN_NORMALIZATION_FAILED
    )


def test_link_targets_are_rejected_when_changed(monkeypatch: pytest.MonkeyPatch) -> None:
    markdown = "## heading\n\n[link](https://example.com/a)\n"

    def _bad_format(*_args: object, **_kwargs: object) -> str:
        return "## heading\n\n[link](https://example.com/bad)\n"

    monkeypatch.setattr(markdown_formatter.mdformat, "text", _bad_format)
    with pytest.raises(MarkdownCleaningProcessorError) as exc:
        MarkdownFormatterAdapter().format(markdown)

    assert getattr(exc.value, "code", None) == MarkdownCleaningErrorCode.MARKDOWN_NORMALIZATION_FAILED


@pytest.mark.parametrize(
    ("source", "formatted", "changes"),
    [
        ("a  \r\nb  \r\n", "a\nb\n", 1),
        ("a\n\nb\n", "\na\n\nb\n", 1),
        ("a   \n\nb   \n", "a\n\nb\n", 2),
        ("a\n\n\n\n", "a\n", 1),
        ("hello\n", "hello\n", 0),
    ],
)
def test_formatting_change_count_golden_vectors(
    source: str,
    formatted: str,
    changes: int,
) -> None:
    assert (
        MarkdownFormatterAdapter._count_formatting_changes(source, formatted)
        == changes
    )


def test_protected_fence_and_inline_code_contents_remain_exact() -> None:
    markdown = (
        "```\n"
        "x = 'a'\n"
        "```\n\n"
        "before `https://example.com?q=1` after\n\n"
    )
    result = MarkdownFormatterAdapter().format(markdown)

    assert _collect_protected_units(markdown) == _collect_protected_units(result.text)


def test_bom_and_trailing_newline_constraints_are_enforced() -> None:
    markdown = "\ufeffhello   \n\n**x**\n"
    with pytest.raises(MarkdownCleaningProcessorError) as exc:
        MarkdownFormatterAdapter().format(markdown)

    assert str(exc.value).startswith("MARKDOWN_NORMALIZATION_FAILED:")
    assert (
        getattr(exc.value, "code", None)
        == MarkdownCleaningErrorCode.MARKDOWN_NORMALIZATION_FAILED
    )
