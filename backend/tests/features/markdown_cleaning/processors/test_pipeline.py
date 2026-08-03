from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from app.features.markdown_cleaning.processors import (
    MarkdownCleaningPipeline,
    MarkdownCleaningPipelineLimits,
    MarkdownCleaningSummary,
)
from app.features.markdown_cleaning.processors.errors import (
    MarkdownCleaningErrorCode,
    MarkdownCleaningProcessorError,
)
from app.features.markdown_cleaning.processors.markdown_formatter import (
    MarkdownFormatterAdapter,
    MarkdownFormatterResult,
)

_BOM = "\ufeff"


def test_pipeline_process_executes_in_order_and_preserves_protection_blocks(tmp_path: Path) -> None:
    source = tmp_path / "fixture.md"
    destination = tmp_path / "out.md"
    source.write_text(
        (
            "# 标题\n"
            "\n"
            "重复段落示例。\n\n"
            "重复段落示例。\n\n"
            "联系邮箱: a@sample.org\n"
            "电话: 13800138000\n"
            "银行卡: 4111111111110006\n"
            "身份证: 11010519491231002X\n"
            "IP: 10.0.0.1\n\n"
            "`13800138000` 与 `https://example.com/path?a=1` 不应被脱敏。\n\n"
            "```text\n"
            "13800138000 10.0.0.1 a@sample.org\n"
            "```\n\n"
            "<div>a@sample.org 13800138000</div>\n\n"
            "|h1|h2|\n"
            "|---|---|\n"
            "|A|B|\n"
            "\n"
            "- item one\n"
            "- item one\n"
        ),
        encoding="utf-8",
        newline="",
    )

    pipeline = MarkdownCleaningPipeline(
        limits=MarkdownCleaningPipelineLimits(
            max_input_bytes=2_000,
            max_output_bytes=2_000,
        )
    )
    result = pipeline.process(source, destination)

    summary = result.summary
    assert summary.duplicate_paragraphs_removed == 1
    assert summary.email_redactions == 1
    assert summary.phone_redactions == 1
    assert summary.bank_card_redactions == 1
    assert summary.id_card_redactions == 1
    assert summary.ipv4_redactions == 1
    assert summary.formatting_changes >= 0
    assert result.contract_version == "markdown_cleaning_v1"
    assert result.output_path == destination
    assert result.input_sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert result.output_sha256 == hashlib.sha256(destination.read_bytes()).hexdigest()
    assert result.input_bytes == len(source.read_bytes())
    assert result.output_bytes == len(destination.read_bytes())
    output = destination.read_text(encoding="utf-8")
    assert _BOM not in output
    assert "联系邮箱: [EMAIL]" in output
    assert "电话: [PHONE]" in output
    assert "`13800138000`" in output
    assert "https://example.com/path?a=1" in output
    assert "<div>a@sample.org 13800138000</div>" in output
    assert "13800138000 10.0.0.1 a@sample.org" in output
    assert "```text" in output


def test_pipeline_second_run_is_idempotent_when_input_is_bytestable_output(tmp_path: Path) -> None:
    source = tmp_path / "first.md"
    first_dest = tmp_path / "first-out.md"
    second_dest = tmp_path / "second-out.md"
    source.write_text(
        (
            "# 标题\n\n"
            "去重段落。\n"
            "去重段落。\n"
            "a@sample.org\n"
        ),
        encoding="utf-8",
        newline="",
    )
    pipeline = MarkdownCleaningPipeline()
    pipeline.process(source, first_dest)
    second = pipeline.process(first_dest, second_dest)

    assert first_dest.read_bytes() == second_dest.read_bytes()
    assert second.summary == MarkdownCleaningSummary(
        duplicate_paragraphs_removed=0,
        phone_redactions=0,
        id_card_redactions=0,
        bank_card_redactions=0,
        email_redactions=0,
        ipv4_redactions=0,
        formatting_changes=0,
    )
    assert second.input_sha256 == hashlib.sha256(first_dest.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    "invalid_bytes",
    [b"\xff", "\ufeffhello\n".encode(), b"hello\x00world\n"],
)
def test_pipeline_rejects_non_utf8_input_and_bom_and_null_bytes(
    tmp_path: Path,
    invalid_bytes: bytes,
) -> None:
    pipeline = MarkdownCleaningPipeline()
    invalid = tmp_path / "invalid.bin"
    destination = tmp_path / "ignore.md"
    invalid.write_bytes(invalid_bytes)
    with pytest.raises(MarkdownCleaningProcessorError) as exc:
        pipeline.process(invalid, destination)
    assert exc.value.code is MarkdownCleaningErrorCode.INVALID_MARKDOWN_INPUT
    assert not destination.exists()


@pytest.mark.parametrize(
    ("markdown", "limits"),
    [
        ("too large\n", MarkdownCleaningPipelineLimits(max_input_bytes=1)),
        ("one\n\ntwo\n", MarkdownCleaningPipelineLimits(max_block_count=1)),
        ("`protected`\n", MarkdownCleaningPipelineLimits(max_protected_span_count=0)),
        ("long block\n", MarkdownCleaningPipelineLimits(max_block_char_span=1)),
        ("# token\n", MarkdownCleaningPipelineLimits(max_token_count=0)),
        ("a@sample.org\n", MarkdownCleaningPipelineLimits(max_pii_candidate_count=0)),
    ],
)
def test_pipeline_rejects_each_resource_limit(
    tmp_path: Path,
    markdown: str,
    limits: MarkdownCleaningPipelineLimits,
) -> None:
    source = tmp_path / "resource.md"
    destination = tmp_path / "resource-out.md"
    source.write_text(markdown, encoding="utf-8", newline="")
    pipeline = MarkdownCleaningPipeline(limits=limits)
    with pytest.raises(MarkdownCleaningProcessorError) as exc:
        pipeline.process(source, destination)
    assert exc.value.code is MarkdownCleaningErrorCode.INVALID_MARKDOWN_INPUT
    assert not destination.exists()


def test_pipeline_rejects_timeout_without_publishing_output(tmp_path: Path) -> None:
    source = tmp_path / "resource.md"
    destination = tmp_path / "timeout-out.md"
    source.write_text("段落内容\n", encoding="utf-8", newline="")

    timings = iter([0.0, 0.0, 3.0])

    def frozen() -> float:
        try:
            return next(timings)
        except StopIteration:
            return 3.0

    pipeline = MarkdownCleaningPipeline(
        limits=MarkdownCleaningPipelineLimits(processing_timeout_seconds=1.0),
        time_fn=frozen,
    )
    with pytest.raises(MarkdownCleaningProcessorError) as exc:
        pipeline.process(source, destination)
    assert exc.value.code is MarkdownCleaningErrorCode.PROCESSING_TIMEOUT
    assert not destination.exists()


def test_pipeline_output_size_limit_is_enforced(tmp_path: Path) -> None:
    source = tmp_path / "huge.md"
    source.write_text("x" * 1024, encoding="utf-8", newline="")
    pipeline = MarkdownCleaningPipeline(
        limits=MarkdownCleaningPipelineLimits(
            max_output_bytes=16,
            max_input_bytes=2_000,
        )
    )
    with pytest.raises(MarkdownCleaningProcessorError) as exc:
        pipeline.process(source, tmp_path / "large-out.md")
    assert exc.value.code is MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_input_bytes", -1),
        ("max_output_bytes", -1),
        ("max_block_count", -1),
        ("max_protected_span_count", -1),
        ("max_block_char_span", -1),
        ("max_token_count", -1),
        ("max_pii_candidate_count", -1),
        ("processing_timeout_seconds", 0),
        ("processing_timeout_seconds", float("inf")),
        ("processing_timeout_seconds", float("nan")),
    ],
)
def test_pipeline_limits_reject_invalid_values(field: str, value: Any) -> None:
    with pytest.raises(ValueError):
        MarkdownCleaningPipelineLimits(**{field: value})


def test_pipeline_preserves_existing_destination_when_stage_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.md"
    destination = tmp_path / "output.md"
    source.write_text("正文\n", encoding="utf-8", newline="")
    destination.write_bytes(b"previous\n")
    formatter = MarkdownFormatterAdapter()

    def fail_format(_markdown: str) -> MarkdownFormatterResult:
        raise RuntimeError("sensitive content must not escape")

    monkeypatch.setattr(formatter, "format", fail_format)
    pipeline = MarkdownCleaningPipeline(formatter=formatter)

    with pytest.raises(MarkdownCleaningProcessorError) as exc:
        pipeline.process(source, destination)

    assert exc.value.code is MarkdownCleaningErrorCode.MARKDOWN_NORMALIZATION_FAILED
    assert destination.read_bytes() == b"previous\n"
    assert list(tmp_path.glob("markdown-cleaning-*.tmp")) == []


def test_pipeline_validates_summary_before_publishing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.md"
    destination = tmp_path / "output.md"
    source.write_text("正文\n", encoding="utf-8", newline="")
    destination.write_bytes(b"previous\n")
    formatter = MarkdownFormatterAdapter()
    monkeypatch.setattr(
        formatter,
        "format",
        lambda markdown: MarkdownFormatterResult(
            text=markdown,
            formatting_changes=-1,
        ),
    )

    with pytest.raises(MarkdownCleaningProcessorError) as exc:
        MarkdownCleaningPipeline(formatter=formatter).process(source, destination)

    assert exc.value.code is MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT
    assert destination.read_bytes() == b"previous\n"


def test_pipeline_preserves_timeout_code_and_destination_during_atomic_write(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.md"
    destination = tmp_path / "output.md"
    source.write_text("正文\n", encoding="utf-8", newline="")
    destination.write_bytes(b"previous\n")

    def time_fn() -> float:
        if list(tmp_path.glob("markdown-cleaning-*.tmp")):
            return 2.0
        return 0.0

    pipeline = MarkdownCleaningPipeline(
        limits=MarkdownCleaningPipelineLimits(processing_timeout_seconds=1.0),
        time_fn=time_fn,
    )

    with pytest.raises(MarkdownCleaningProcessorError) as exc:
        pipeline.process(source, destination)

    assert exc.value.code is MarkdownCleaningErrorCode.PROCESSING_TIMEOUT
    assert destination.read_bytes() == b"previous\n"
    assert list(tmp_path.glob("markdown-cleaning-*.tmp")) == []


@pytest.mark.parametrize("invalid_output", ["", "text\r\n", "text\n\n"])
def test_pipeline_rejects_invalid_formatter_output_before_atomic_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_output: str,
) -> None:
    source = tmp_path / "source.md"
    destination = tmp_path / "output.md"
    source.write_text("正文\n", encoding="utf-8", newline="")
    formatter = MarkdownFormatterAdapter()
    monkeypatch.setattr(
        formatter,
        "format",
        lambda _markdown: MarkdownFormatterResult(
            text=invalid_output,
            formatting_changes=1,
        ),
    )

    with pytest.raises(MarkdownCleaningProcessorError) as exc:
        MarkdownCleaningPipeline(formatter=formatter).process(source, destination)

    assert exc.value.code is MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT
    assert not destination.exists()


def test_pipeline_deduplicates_before_redaction(tmp_path: Path) -> None:
    source = tmp_path / "source.md"
    destination = tmp_path / "output.md"
    source.write_text(
        "电话: 13800138000\n\n电话: 13800138000\n\n电话: 13900139000\n",
        encoding="utf-8",
        newline="",
    )

    result = MarkdownCleaningPipeline().process(source, destination)

    assert result.summary.duplicate_paragraphs_removed == 1
    assert result.summary.phone_redactions == 2
    assert destination.read_text(encoding="utf-8").count("电话: [PHONE]") == 2
