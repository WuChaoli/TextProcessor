from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from app.features.markdown_cleaning.processors import (
    MarkdownCleaningPipeline,
    MarkdownCleaningPipelineLimits,
    MarkdownCleaningSummary,
)

_ROOT = Path(__file__).resolve().parents[3] / "fixtures" / "markdown_cleaning" / "v1"


def test_markdown_cleaning_golden_corpus_bytes_summary_and_idempotency(
    tmp_path: Path,
) -> None:
    cases = sorted(item for item in _ROOT.iterdir() if item.is_dir())
    assert cases, "golden corpus should contain at least one case"

    pipeline = MarkdownCleaningPipeline(
        staging_root=tmp_path,
        limits=MarkdownCleaningPipelineLimits(
            max_input_bytes=10_485_760, max_output_bytes=10_485_760
        ),
    )

    for case_dir in cases:
        fixture_source = case_dir / "input.md"
        case_staging = tmp_path / case_dir.name
        case_staging.mkdir()
        source = case_staging / "input.md"
        source.write_bytes(fixture_source.read_bytes())
        expected = case_dir / "expected.md"
        expected_summary = case_dir / "summary.json"

        output = case_staging / "output.md"
        rerun_output = case_staging / "rerun-output.md"
        first = pipeline.process(source, output)

        actual_summary = asdict(first.summary)
        expected_summary_dict = json.loads(expected_summary.read_text(encoding="utf-8"))

        assert output.read_bytes() == expected.read_bytes()
        assert actual_summary == expected_summary_dict
        assert first.input_sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
        assert first.output_sha256 == hashlib.sha256(expected.read_bytes()).hexdigest()
        assert first.input_bytes == len(source.read_bytes())
        assert first.output_bytes == len(expected.read_bytes())

        second = pipeline.process(output, rerun_output)
        assert rerun_output.read_bytes() == expected.read_bytes()
        assert second.summary == MarkdownCleaningSummary(
            duplicate_paragraphs_removed=0,
            phone_redactions=0,
            id_card_redactions=0,
            bank_card_redactions=0,
            email_redactions=0,
            ipv4_redactions=0,
            formatting_changes=0,
        )
