from __future__ import annotations

from pathlib import Path

from app.features.markdown_cleaning.processors import MarkdownCleaningPipeline


def test_markdown_cleaning_pipeline_processes_duplicate_and_redacts(tmp_path: Path) -> None:
    source_path = tmp_path / "in.md"
    destination_path = tmp_path / "out.md"
    source_path.write_text(
        "Alpha paragraph with duplicates.\n\n"
        "Duplicate paragraph.\n\n"
        "Duplicate paragraph.\n\n"
        "Email: alice@example.com\n"
        "Phone: 13800138000\n"
        "Card: 4012888888881881\n"
        "IP: 8.8.8.8\n",
        encoding="utf-8",
    )

    result = MarkdownCleaningPipeline().process(
        source_path=source_path,
        destination_path=destination_path,
    )

    output = destination_path.read_text(encoding="utf-8")
    assert "alice@example.com" not in output
    assert "13800138000" not in output
    assert "4012888888881881" not in output
    assert "8.8.8.8" not in output

    assert result.summary.duplicate_paragraphs_removed == 1
    assert result.summary.email_redactions == 1
    assert result.summary.phone_redactions == 1
    assert result.summary.bank_card_redactions == 1
    assert result.summary.ipv4_redactions == 1
    assert result.summary.id_card_redactions == 0
    assert result.output_path == destination_path
    assert result.input_sha256
    assert result.output_sha256


def test_markdown_cleaning_pipeline_missing_source_raises(tmp_path: Path) -> None:
    missing = tmp_path / "missing.md"
    destination = tmp_path / "out.md"

    try:
        MarkdownCleaningPipeline().process(source_path=missing, destination_path=destination)
    except OSError:
        return

    raise AssertionError("expected OSError for missing source")
