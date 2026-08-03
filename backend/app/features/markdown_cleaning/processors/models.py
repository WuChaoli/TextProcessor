from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path
from re import fullmatch
from typing import Literal

MarkdownCleaningContractVersion = Literal["markdown_cleaning_v1"]


def _validate_non_negative(value: int, name: str) -> None:
    if value < 0:
        raise ValueError(f"{name} must be >= 0")


def _validate_sha256(value: str, name: str) -> None:
    if len(value) != 64 or fullmatch(r"[0-9a-fA-F]{64}", value) is None:
        raise ValueError(f"{name} must be a hex sha256")


def _validate_non_negative_bytes(value: int, name: str) -> None:
    if value < 0:
        raise ValueError(f"{name} must be >= 0")


@dataclass(frozen=True, slots=True)
class SourceSpan:
    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start < 0:
            raise ValueError("start 不能为负数")
        if self.end < self.start:
            raise ValueError("end 不能小于 start")
        if self.end < 0:
            raise ValueError("end 不能为负数")


@dataclass(frozen=True, slots=True)
class MarkdownCleaningSummary:
    duplicate_paragraphs_removed: int
    phone_redactions: int
    id_card_redactions: int
    bank_card_redactions: int
    email_redactions: int
    ipv4_redactions: int
    formatting_changes: int

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if not isinstance(value, int):
                raise ValueError(f"{field.name} must be int")
            _validate_non_negative(value, field.name)


@dataclass(frozen=True, slots=True)
class ProcessorResult:
    output_path: Path
    input_sha256: str
    output_sha256: str
    contract_version: MarkdownCleaningContractVersion
    summary: MarkdownCleaningSummary
    input_bytes: int = 0
    output_bytes: int = 0

    def __post_init__(self) -> None:
        _validate_sha256(self.input_sha256, "input_sha256")
        _validate_sha256(self.output_sha256, "output_sha256")
        _validate_non_negative_bytes(self.input_bytes, "input_bytes")
        _validate_non_negative_bytes(self.output_bytes, "output_bytes")
        if not isinstance(self.output_path, Path):
            raise ValueError("output_path must be Path")
        if not isinstance(self.summary, MarkdownCleaningSummary):
            raise ValueError("summary must be MarkdownCleaningSummary")
