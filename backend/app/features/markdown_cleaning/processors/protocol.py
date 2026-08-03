from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from app.features.markdown_cleaning.processors.models import ProcessorResult


@runtime_checkable
class MarkdownCleaningProcessor(Protocol):
    def process(self, source_path: Path, destination_path: Path) -> ProcessorResult:
        ...
