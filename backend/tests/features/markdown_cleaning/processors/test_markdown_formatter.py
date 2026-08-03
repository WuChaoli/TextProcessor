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
    MarkdownInlineLeafType,
)


def _collect_block_signature(markdown: str) -> tuple[MarkdownBlockType, ...]:
    parsed = MarkdownParserAdapter().parse(markdown)
    return tuple(
        block.block_type
        for block in parsed.blocks
        if block.block_type is not MarkdownBlockType.BLANK
    )


def _collect_link_targets(markdown: str) -> tuple[str, ...]:
    parsed = MarkdownParserAdapter().parse(markdown)
    return tuple(
        markdown[leaf.source_span.start : leaf.source_span.end]
        for leaf in parsed.inline_leaves
        if leaf.kind
        in {
            MarkdownInlineLeafType.LINK_DESTINATION,
            MarkdownInlineLeafType.IMAGE_DESTINATION,
        }
    )

def _normalize_protected_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _collect_protected_units(markdown: str) -> tuple[tuple[str, str, str], ...]:
    parsed = MarkdownParserAdapter().parse(markdown)
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
                        _normalize_protected_text(
                            markdown[block.source_span.start : block.source_span.end]
                        ),
                    )
            )

    for leaf in parsed.inline_leaves:
        if leaf.kind in {
            MarkdownInlineLeafType.CODE_INLINE,
            MarkdownInlineLeafType.HTML_INLINE,
            MarkdownInlineLeafType.LINK_DESTINATION,
            MarkdownInlineLeafType.IMAGE_DESTINATION,
        }:
            units.append(
                    (
                        leaf.source_span.start,
                        leaf.kind.value,
                        leaf.parent_block_kind.value,
                        _normalize_protected_text(
                            markdown[leaf.source_span.start : leaf.source_span.end]
                        ),
                    )
                )

    units.sort(key=lambda item: item[0])
    return tuple(item[1:] for item in units)


def test_markdown_formatter_preserves_ast_and_protected_units_and_links() -> None:
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
    )

    formatter = MarkdownFormatterAdapter()
    result = formatter.format(markdown)

    assert result.formatting_changes >= 1
    assert not result.text.startswith("\ufeff")
    assert "\r" not in result.text
    assert result.text.endswith("\n")
    assert not result.text.endswith("\n\n")
    assert _collect_block_signature(markdown) == _collect_block_signature(result.text)
    assert _collect_protected_units(markdown) == _collect_protected_units(result.text)
    assert _collect_link_targets(markdown) == _collect_link_targets(result.text)


def test_formatting_second_pass_is_byte_stable() -> None:
    markdown = "##  Title  \n\n-   item one  \n\n```text\nx = 1\n```\n"
    formatter = MarkdownFormatterAdapter()
    first = formatter.format(markdown)
    second = formatter.format(first.text)

    assert first.text == second.text
    assert second.formatting_changes == 0


def test_disallowed_structure_change_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    markdown = "### heading\n\n- item\n\n[link](https://example.com/a)\n"

    def _bad_format(*_args: object, **_kwargs: object) -> str:
        return "para changed by bad formatter\n"

    monkeypatch.setattr(markdown_formatter.mdformat, "text", _bad_format)
    with pytest.raises(MarkdownCleaningProcessorError) as exc:
        MarkdownFormatterAdapter().format(markdown)

    assert getattr(exc.value, "code", None) is MarkdownCleaningErrorCode.MARKDOWN_NORMALIZATION_FAILED


def test_link_targets_are_rejected_when_changed(monkeypatch: pytest.MonkeyPatch) -> None:
    markdown = "## heading\n\n[link](https://example.com/a)\n"

    def _bad_format(*_args: object, **_kwargs: object) -> str:
        return "## heading\n\n[link](https://example.com/bad)\n"

    monkeypatch.setattr(markdown_formatter.mdformat, "text", _bad_format)
    with pytest.raises(MarkdownCleaningProcessorError) as exc:
        MarkdownFormatterAdapter().format(markdown)

    assert getattr(exc.value, "code", None) == MarkdownCleaningErrorCode.MARKDOWN_NORMALIZATION_FAILED


def test_formatting_change_count_tracks_continuous_changed_regions() -> None:
    source = "a   \n\nb   \n"
    formatted = "a\n\nb\n"
    assert MarkdownFormatterAdapter._count_formatting_changes(source, formatted) == 2


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
