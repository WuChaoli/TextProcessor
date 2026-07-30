import re
from collections.abc import Iterable
from html.parser import HTMLParser
from typing import Any

import mistune
from mistune.core import BlockState
from mistune.renderers.markdown import MarkdownRenderer

from app.features.structured_extraction.errors import (
    ExtractionErrorCode,
    ExtractionProcessingError,
)

_REFERENCE_DEFINITION = re.compile(r"^[ ]{0,3}\[[^\]]+\]:[ \t]+\S")


class _ImageRemovingHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.parts: list[str] = []
        self.found_image = False

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag.lower() == "img":
            self.found_image = True
            alt = next(
                (value for name, value in attrs if name.lower() == "alt"),
                None,
            )
            if alt:
                self.parts.append(alt)
            return
        self.parts.append(self.get_starttag_text() or f"<{tag}>")

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "img":
            self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_entityref(self, name: str) -> None:
        self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.parts.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        self.parts.append(f"<!--{data}-->")

    @property
    def output(self) -> str:
        return "".join(self.parts)


def _clean_html(raw: str) -> str:
    parser = _ImageRemovingHTMLParser()
    parser.feed(raw)
    parser.close()
    return parser.output


class _ImageRemovingMarkdownRenderer(MarkdownRenderer):
    def image(self, token: dict[str, Any], state: BlockState) -> str:
        return self.render_children(token, state)

    def inline_html(self, token: dict[str, Any], state: BlockState) -> str:
        return _clean_html(str(token["raw"]))

    def block_html(self, token: dict[str, Any], state: BlockState) -> str:
        return _clean_html(str(token["raw"])) + "\n\n"

    def inline_math(self, token: dict[str, Any], state: BlockState) -> str:
        return f"${token['raw']}$"

    def block_math(self, token: dict[str, Any], state: BlockState) -> str:
        return f"$$\n{token['raw']}\n$$\n\n"

    def table(self, token: dict[str, Any], state: BlockState) -> str:
        return self.render_children(token, state).rstrip() + "\n\n"

    def table_head(self, token: dict[str, Any], state: BlockState) -> str:
        cells = token.get("children", [])
        header = self._render_table_row(cells, state)
        separators: list[str] = []
        for cell in cells:
            attrs = cell.get("attrs", {})
            align = attrs.get("align") if isinstance(attrs, dict) else None
            separators.append(
                {
                    "left": ":---",
                    "center": ":---:",
                    "right": "---:",
                }.get(str(align), "---")
            )
        return header + "| " + " | ".join(separators) + " |\n"

    def table_body(self, token: dict[str, Any], state: BlockState) -> str:
        return self.render_children(token, state)

    def table_row(self, token: dict[str, Any], state: BlockState) -> str:
        return self._render_table_row(token.get("children", []), state)

    def table_cell(self, token: dict[str, Any], state: BlockState) -> str:
        return self.render_children(token, state)

    def _render_table_row(
        self,
        cells: list[dict[str, Any]],
        state: BlockState,
    ) -> str:
        rendered = [
            self.render_children(cell, state).replace("|", r"\|").strip()
            for cell in cells
        ]
        return "| " + " | ".join(rendered) + " |\n"

    def __call__(
        self,
        tokens: Iterable[dict[str, Any]],
        state: BlockState,
    ) -> str:
        materialized = list(tokens)
        image_refs: set[str] = set()
        link_refs: set[str] = set()
        self._collect_refs(materialized, image_refs, link_refs)
        references = state.env.get("ref_links")
        if isinstance(references, dict):
            for reference in image_refs - link_refs:
                references.pop(reference, None)
        return super().__call__(materialized, state)

    @classmethod
    def _collect_refs(
        cls,
        tokens: list[dict[str, Any]],
        image_refs: set[str],
        link_refs: set[str],
    ) -> None:
        for token in tokens:
            reference = token.get("ref")
            if isinstance(reference, str):
                if token.get("type") == "image":
                    image_refs.add(reference)
                elif token.get("type") == "link":
                    link_refs.add(reference)
            children = token.get("children")
            if isinstance(children, list):
                cls._collect_refs(children, image_refs, link_refs)


class MarkdownNormalizer:
    def __init__(self) -> None:
        plugins = ["table", "math"]
        self._renderer = _ImageRemovingMarkdownRenderer()
        self._markdown = mistune.create_markdown(
            renderer=self._renderer,
            plugins=plugins,
        )
        self._ast = mistune.create_markdown(renderer="ast", plugins=plugins)

    def normalize(self, markdown: str) -> str:
        try:
            prepared_markdown = self._separate_reference_definitions(markdown)
            normalized = str(self._markdown(prepared_markdown))
            tokens = self._ast(normalized)
        except Exception:
            raise invalid_output() from None
        if not isinstance(tokens, list) or self._contains_forbidden_content(tokens):
            raise invalid_output()
        return normalized

    @staticmethod
    def _separate_reference_definitions(markdown: str) -> str:
        lines = markdown.splitlines(keepends=True)
        output: list[str] = []
        for line in lines:
            if _REFERENCE_DEFINITION.match(line) and output and output[-1].strip():
                output.append("\n")
            output.append(line)
        return "".join(output)

    @classmethod
    def _contains_forbidden_content(cls, tokens: list[dict[str, Any]]) -> bool:
        for token in tokens:
            token_type = token.get("type")
            if token_type == "image":
                return True
            if token_type == "link":
                attrs = token.get("attrs")
                if isinstance(attrs, dict) and str(
                    attrs.get("url", "")
                ).lower().startswith("data:image"):
                    return True
            if token_type in {"inline_html", "block_html"}:
                parser = _ImageRemovingHTMLParser()
                parser.feed(str(token.get("raw", "")))
                if parser.found_image:
                    return True
            children = token.get("children")
            if isinstance(children, list) and cls._contains_forbidden_content(children):
                return True
        return False


def invalid_output() -> ExtractionProcessingError:
    return ExtractionProcessingError(
        ExtractionErrorCode.INVALID_PROCESSOR_OUTPUT,
        "处理器输出包含不允许的图片内容或 Markdown 无效",
    )
