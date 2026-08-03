from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

MarkdownCleaningContractVersion = Literal["markdown_cleaning_v1"]


@dataclass(frozen=True, slots=True)
class SourceSpan:
    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start < 0:
            raise ValueError("start 不能为负数")
        if self.end < self.start:
            raise ValueError("end 不能小于 start")


@dataclass(frozen=True, slots=True)
class MarkdownCleaningSummary:
    duplicate_paragraphs_removed: int
    phone_redactions: int
    id_card_redactions: int
    bank_card_redactions: int
    email_redactions: int
    ipv4_redactions: int
    formatting_changes: int


@dataclass(frozen=True, slots=True)
class ProcessorResult:
    output_path: Path
    input_sha256: str
    output_sha256: str
    contract_version: MarkdownCleaningContractVersion
    summary: MarkdownCleaningSummary
