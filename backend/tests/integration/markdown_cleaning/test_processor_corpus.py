from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TypedDict

import pytest

from app.features.markdown_cleaning.processors import MarkdownCleaningPipeline
from app.features.markdown_cleaning.processors.models import MarkdownCleaningSummary


@pytest.fixture(scope="session", autouse=True)
def db() -> None:
    """Corpus integration tests do not depend on Postgres."""


class _CorpusCase(TypedDict):
    input_path: Path
    expected_path: Path
    summary: dict[str, int]


def _load_case(case_name: str, base: Path) -> _CorpusCase:
    fixture_root = base / "fixtures" / "markdown_cleaning" / "v1" / case_name
    input_path = fixture_root / "input.md"
    expected_path = fixture_root / "expected.md"
    summary_path = fixture_root / "summary.json"

    if not input_path.exists() or not expected_path.exists() or not summary_path.exists():
        pytest.fail(f"fixture missing: {case_name}")

    with summary_path.open(encoding="utf-8") as fp:
        raw_summary = json.load(fp)

    return {
        "input_path": input_path,
        "expected_path": expected_path,
        "summary": raw_summary,
    }


@pytest.mark.parametrize(
    "case_name",
    (
        "case-duplicate-redact",
        "case-gfm-format",
    ),
)
def test_markdown_cleaning_pipeline_corpus(case_name: str, tmp_path: Path) -> None:
    fixture_root = Path(__file__).resolve().parent.parent.parent
    case = _load_case(case_name=case_name, base=fixture_root)
    input_path = case["input_path"]
    expected_path = case["expected_path"]
    expected_summary = case["summary"]

    output_path = tmp_path / f"{case_name}.md"
    result = MarkdownCleaningPipeline().process(
        source_path=input_path,
        destination_path=output_path,
    )

    actual_text = output_path.read_text(encoding="utf-8")
    expected_text = expected_path.read_text(encoding="utf-8")

    assert actual_text == expected_text
    expected = MarkdownCleaningSummary(**expected_summary)
    assert result.summary == expected

    input_sha256 = hashlib.sha256(input_path.read_bytes()).hexdigest()
    assert result.input_sha256 == input_sha256
    assert result.output_path == output_path
