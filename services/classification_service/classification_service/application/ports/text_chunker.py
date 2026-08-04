from typing import Protocol


class TextChunker(Protocol):
    """Split a document into the bounded text chunks used by classifiers."""

    def chunk(self, text: str) -> tuple[str, ...]: ...
