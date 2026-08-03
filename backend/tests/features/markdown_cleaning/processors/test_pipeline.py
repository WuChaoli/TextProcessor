from __future__ import annotations

import hashlib
import os
import sys
import time
from datetime import UTC, datetime, timedelta
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
from app.features.markdown_cleaning.processors.markdown_parser import (
    MarkdownParserAdapter,
    MarkdownParserError,
    MarkdownParserErrorCode,
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
        staging_root=tmp_path,
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
    pipeline = MarkdownCleaningPipeline(staging_root=tmp_path)
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


def test_pipeline_preserves_autolink_and_reference_destinations(
    tmp_path: Path,
) -> None:
    source = tmp_path / "links.md"
    destination = tmp_path / "links-out.md"
    source.write_text(
        (
            "ordinary a@b.com and 10.0.0.1\n\n"
            "<mailto:a@b.com> and <https://10.0.0.1/path>\n\n"
            "[safe label][id]\n\n"
            "[id]: https://a@b.com/foo(1)\n"
        ),
        encoding="utf-8",
        newline="",
    )

    result = MarkdownCleaningPipeline(staging_root=tmp_path).process(
        source,
        destination,
        deadline=datetime.now(UTC) + timedelta(seconds=30),
    )

    output = destination.read_text(encoding="utf-8")
    assert "ordinary [EMAIL] and [IPV4]" in output
    assert "<mailto:a@b.com>" in output
    assert "<https://10.0.0.1/path>" in output
    assert "[id]: https://a@b.com/foo(1)" in output
    assert result.summary.email_redactions == 1
    assert result.summary.ipv4_redactions == 1


@pytest.mark.parametrize(
    "invalid_bytes",
    [b"\xff", "\ufeffhello\n".encode(), b"hello\x00world\n"],
)
def test_pipeline_rejects_non_utf8_input_and_bom_and_null_bytes(
    tmp_path: Path,
    invalid_bytes: bytes,
) -> None:
    pipeline = MarkdownCleaningPipeline(staging_root=tmp_path)
    invalid = tmp_path / "invalid.bin"
    destination = tmp_path / "ignore.md"
    invalid.write_bytes(invalid_bytes)
    with pytest.raises(MarkdownCleaningProcessorError) as exc:
        pipeline.process(invalid, destination)
    assert exc.value.code is MarkdownCleaningErrorCode.INVALID_MARKDOWN_INPUT
    assert not destination.exists()


def test_pipeline_maps_unclosed_fence_to_invalid_markdown_input(tmp_path: Path) -> None:
    source = tmp_path / "unclosed.md"
    destination = tmp_path / "output.md"
    source.write_text("```text\nsecret\n", encoding="utf-8", newline="")

    with pytest.raises(MarkdownCleaningProcessorError) as exc:
        MarkdownCleaningPipeline(staging_root=tmp_path).process(source, destination)

    assert exc.value.code is MarkdownCleaningErrorCode.INVALID_MARKDOWN_INPUT
    assert not destination.exists()


def test_pipeline_preserves_true_parser_failure_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.md"
    destination = tmp_path / "output.md"
    source.write_text("正文\n", encoding="utf-8", newline="")
    parser = MarkdownParserAdapter()

    def fail_parse(_markdown: str) -> None:
        raise MarkdownParserError(
            MarkdownParserErrorCode.MARKDOWN_PARSE_FAILED,
            "sensitive parser detail",
        )

    monkeypatch.setattr(parser, "parse", fail_parse)

    with pytest.raises(MarkdownCleaningProcessorError) as exc:
        MarkdownCleaningPipeline(
            staging_root=tmp_path,
            parser=parser,
            _run_inline=True,
        ).process(
            source, destination
        )

    assert exc.value.code is MarkdownCleaningErrorCode.MARKDOWN_PARSE_FAILED
    assert "sensitive parser detail" not in exc.value.safe_message
    assert not destination.exists()


def test_pipeline_rejects_symlink_source_even_when_target_stays_in_staging(
    tmp_path: Path,
) -> None:
    actual_source = tmp_path / "actual.md"
    source_link = tmp_path / "source-link.md"
    destination = tmp_path / "output.md"
    actual_source.write_text("正文\n", encoding="utf-8", newline="")
    try:
        source_link.symlink_to(actual_source)
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")

    with pytest.raises(MarkdownCleaningProcessorError) as error:
        MarkdownCleaningPipeline(staging_root=tmp_path).process(source_link, destination)

    assert error.value.code is MarkdownCleaningErrorCode.INVALID_MARKDOWN_INPUT
    assert not destination.exists()


def test_pipeline_rejects_symlink_destination_parent_inside_staging(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.md"
    real_output_dir = tmp_path / "real-output"
    output_link = tmp_path / "output-link"
    source.write_text("正文\n", encoding="utf-8", newline="")
    real_output_dir.mkdir()
    try:
        output_link.symlink_to(real_output_dir, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")
    destination = output_link / "output.md"

    with pytest.raises(MarkdownCleaningProcessorError) as error:
        MarkdownCleaningPipeline(staging_root=tmp_path).process(source, destination)

    assert error.value.code is MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT
    assert not (real_output_dir / "output.md").exists()


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
    pipeline = MarkdownCleaningPipeline(staging_root=tmp_path, limits=limits)
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
        staging_root=tmp_path,
        limits=MarkdownCleaningPipelineLimits(processing_timeout_seconds=1.0),
        time_fn=frozen,
        _run_inline=True,
    )
    with pytest.raises(MarkdownCleaningProcessorError) as exc:
        pipeline.process(source, destination)
    assert exc.value.code is MarkdownCleaningErrorCode.PROCESSING_TIMEOUT
    assert not destination.exists()


def test_pipeline_terminates_long_transform_within_deadline_without_artifacts(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.md"
    destination = tmp_path / "output.md"
    runtime_marker = tmp_path / "runtime-finished"
    source.write_text("正文\n", encoding="utf-8", newline="")
    runtime_script = (
        "import time;from pathlib import Path;time.sleep(0.4);"
        f"Path({str(runtime_marker)!r}).write_text('finished', encoding='utf-8')"
    )
    pipeline = MarkdownCleaningPipeline(
        staging_root=tmp_path,
        limits=MarkdownCleaningPipelineLimits(processing_timeout_seconds=0.1),
        _runtime_command=(
            sys.executable,
            "-c",
            runtime_script,
        ),
    )

    started_at = time.perf_counter()
    with pytest.raises(MarkdownCleaningProcessorError) as exc:
        pipeline.process(
            source,
            destination,
            deadline=datetime.now(UTC) + timedelta(seconds=5),
        )
    elapsed = time.perf_counter() - started_at

    assert exc.value.code is MarkdownCleaningErrorCode.PROCESSING_TIMEOUT
    assert elapsed < 1.0
    time.sleep(0.5)
    assert not destination.exists()
    assert not runtime_marker.exists()
    assert list(tmp_path.glob("markdown-cleaning-*.tmp")) == []


def test_pipeline_rejects_expired_caller_deadline_before_path_access(
    tmp_path: Path,
) -> None:
    pipeline = MarkdownCleaningPipeline(staging_root=tmp_path)

    with pytest.raises(MarkdownCleaningProcessorError) as exc:
        pipeline.process(
            tmp_path / "missing-source.md",
            tmp_path / "missing-parent" / "output.md",
            deadline=datetime.now(UTC) - timedelta(seconds=1),
        )

    assert exc.value.code is MarkdownCleaningErrorCode.PROCESSING_TIMEOUT
    assert not (tmp_path / "missing-parent").exists()


def test_pipeline_uses_earlier_caller_deadline_for_subprocess_timeout(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.md"
    destination = tmp_path / "output.md"
    runtime_marker = tmp_path / "runtime-finished"
    source.write_text("正文\n", encoding="utf-8", newline="")
    runtime_script = (
        "import time;from pathlib import Path;time.sleep(1.0);"
        f"Path({str(runtime_marker)!r}).write_text('finished', encoding='utf-8')"
    )
    pipeline = MarkdownCleaningPipeline(
        staging_root=tmp_path,
        limits=MarkdownCleaningPipelineLimits(processing_timeout_seconds=5.0),
        _runtime_command=(sys.executable, "-c", runtime_script),
    )

    started_at = time.perf_counter()
    with pytest.raises(MarkdownCleaningProcessorError) as exc:
        pipeline.process(
            source,
            destination,
            deadline=datetime.now(UTC) + timedelta(seconds=0.2),
        )
    elapsed = time.perf_counter() - started_at

    assert exc.value.code is MarkdownCleaningErrorCode.PROCESSING_TIMEOUT
    assert elapsed < 0.8
    time.sleep(0.2)
    assert not destination.exists()
    assert not runtime_marker.exists()
    assert list(tmp_path.glob("markdown-cleaning-*.tmp")) == []


def test_pipeline_rejects_non_utc_deadline(tmp_path: Path) -> None:
    source = tmp_path / "source.md"
    source.write_text("正文\n", encoding="utf-8", newline="")

    with pytest.raises(MarkdownCleaningProcessorError) as exc:
        MarkdownCleaningPipeline(staging_root=tmp_path).process(
            source,
            tmp_path / "output.md",
            deadline=datetime.now(),
        )

    assert exc.value.code is MarkdownCleaningErrorCode.INVALID_MARKDOWN_INPUT


def test_pipeline_rejects_silent_inline_execution_for_custom_stage(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="explicit inline"):
        MarkdownCleaningPipeline(
            staging_root=tmp_path,
            parser=MarkdownParserAdapter(),
        )


def test_pipeline_does_not_forward_application_secrets_to_transform_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.md"
    destination = tmp_path / "output.md"
    source.write_text("正文\n", encoding="utf-8", newline="")
    monkeypatch.setenv("TASK6_RUNTIME_SECRET", "must-not-leak")
    response_script = (
        "import json,os,sys;sys.stdin.buffer.read();"
        "text=('leaked' if os.getenv('TASK6_RUNTIME_SECRET') else 'clean')+'\\n';"
        "sys.stdout.write(json.dumps({'ok':True,'text':text,'duplicateCount':0,"
        "'redactionSummary':{'phone':0,'idCard':0,'bankCard':0,'email':0,'ipv4':0},"
        "'formattingChanges':0}))"
    )
    pipeline = MarkdownCleaningPipeline(
        staging_root=tmp_path,
        _runtime_command=(sys.executable, "-c", response_script),
    )

    pipeline.process(source, destination)

    assert destination.read_bytes() == b"clean\n"


def test_pipeline_output_size_limit_is_enforced(tmp_path: Path) -> None:
    source = tmp_path / "huge.md"
    source.write_text("x" * 1024, encoding="utf-8", newline="")
    pipeline = MarkdownCleaningPipeline(
        staging_root=tmp_path,
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
    pipeline = MarkdownCleaningPipeline(
        staging_root=tmp_path,
        formatter=formatter,
        _run_inline=True,
    )

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
        MarkdownCleaningPipeline(
            staging_root=tmp_path,
            formatter=formatter,
            _run_inline=True,
        ).process(source, destination)

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
        staging_root=tmp_path,
        limits=MarkdownCleaningPipelineLimits(processing_timeout_seconds=1.0),
        time_fn=time_fn,
        _run_inline=True,
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
        MarkdownCleaningPipeline(
            staging_root=tmp_path,
            formatter=formatter,
            _run_inline=True,
        ).process(source, destination)

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

    result = MarkdownCleaningPipeline(staging_root=tmp_path).process(source, destination)

    assert result.summary.duplicate_paragraphs_removed == 1
    assert result.summary.phone_redactions == 2
    assert destination.read_text(encoding="utf-8").count("电话: [PHONE]") == 2


def test_pipeline_rejects_business_target_outside_staging_root(tmp_path: Path) -> None:
    staging_root = tmp_path / "staging"
    staging_root.mkdir()
    source = staging_root / "source.md"
    source.write_text("正文\n", encoding="utf-8", newline="")
    business_target = tmp_path / "business" / "target.md"
    business_target.parent.mkdir()

    pipeline = MarkdownCleaningPipeline(staging_root=staging_root)

    with pytest.raises(MarkdownCleaningProcessorError) as exc:
        pipeline.process(source, business_target)

    assert exc.value.code is MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT
    assert not business_target.exists()


def test_pipeline_rejects_source_outside_staging_root(tmp_path: Path) -> None:
    staging_root = tmp_path / "staging"
    staging_root.mkdir()
    source = tmp_path / "business-input.md"
    destination = staging_root / "output.md"
    source.write_text("正文\n", encoding="utf-8", newline="")

    with pytest.raises(MarkdownCleaningProcessorError) as exc:
        MarkdownCleaningPipeline(staging_root=staging_root).process(source, destination)

    assert exc.value.code is MarkdownCleaningErrorCode.INVALID_MARKDOWN_INPUT
    assert not destination.exists()


def test_pipeline_rejects_lexical_destination_escape(tmp_path: Path) -> None:
    staging_root = tmp_path / "staging"
    outside_dir = tmp_path / "outside"
    staging_root.mkdir()
    outside_dir.mkdir()
    source = staging_root / "source.md"
    source.write_text("正文\n", encoding="utf-8", newline="")
    escaped_destination = staging_root / ".." / "outside" / "escaped.md"

    with pytest.raises(MarkdownCleaningProcessorError) as exc:
        MarkdownCleaningPipeline(staging_root=staging_root).process(
            source, escaped_destination
        )

    assert exc.value.code is MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT
    assert not (outside_dir / "escaped.md").exists()


def test_pipeline_rejects_source_as_destination_without_modifying_it(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.md"
    original = "正文\n".encode()
    source.write_bytes(original)

    with pytest.raises(MarkdownCleaningProcessorError) as exc:
        MarkdownCleaningPipeline(staging_root=tmp_path).process(source, source)

    assert exc.value.code is MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT
    assert source.read_bytes() == original


def test_pipeline_rejects_existing_hard_link_destination_alias(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.md"
    destination = tmp_path / "source-alias.md"
    original = "正文\n".encode()
    source.write_bytes(original)
    os.link(source, destination)
    assert source.samefile(destination)

    with pytest.raises(MarkdownCleaningProcessorError) as exc:
        MarkdownCleaningPipeline(staging_root=tmp_path).process(source, destination)

    assert exc.value.code is MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT
    assert source.read_bytes() == original
    assert destination.read_bytes() == original


def test_pipeline_rechecks_hard_link_alias_immediately_before_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.md"
    destination = tmp_path / "late-alias.md"
    original = "正文\n".encode()
    source.write_bytes(original)
    formatter = MarkdownFormatterAdapter()
    original_format = formatter.format

    def create_alias_before_publish(markdown: str) -> MarkdownFormatterResult:
        result = original_format(markdown)
        os.link(source, destination)
        assert source.samefile(destination)
        return result

    monkeypatch.setattr(formatter, "format", create_alias_before_publish)

    with pytest.raises(MarkdownCleaningProcessorError) as exc:
        MarkdownCleaningPipeline(
            staging_root=tmp_path,
            formatter=formatter,
            _run_inline=True,
        ).process(source, destination)

    assert exc.value.code is MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT
    assert source.read_bytes() == original
    assert destination.read_bytes() == original
    assert list(tmp_path.glob("markdown-cleaning-*.tmp")) == []


def test_pipeline_maps_samefile_os_error_without_publishing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.md"
    destination = tmp_path / "output.md"
    original = "正文\n".encode()
    source.write_bytes(original)

    def fail_samefile(_self: Path, _other: Path) -> bool:
        raise PermissionError("sensitive path detail")

    monkeypatch.setattr(Path, "samefile", fail_samefile)

    with pytest.raises(MarkdownCleaningProcessorError) as exc:
        MarkdownCleaningPipeline(staging_root=tmp_path).process(source, destination)

    assert exc.value.code is MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT
    assert "sensitive path detail" not in exc.value.safe_message
    assert source.read_bytes() == original
    assert not destination.exists()


def test_pipeline_does_not_create_unowned_destination_directories(tmp_path: Path) -> None:
    source = tmp_path / "source.md"
    missing_parent = tmp_path / "unowned"
    destination = missing_parent / "output.md"
    source.write_text("正文\n", encoding="utf-8", newline="")

    with pytest.raises(MarkdownCleaningProcessorError) as exc:
        MarkdownCleaningPipeline(staging_root=tmp_path).process(source, destination)

    assert exc.value.code is MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT
    assert not missing_parent.exists()
