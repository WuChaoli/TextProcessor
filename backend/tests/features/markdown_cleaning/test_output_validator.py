import hashlib
from pathlib import Path

import pytest

from app.features.markdown_cleaning.output_validator import (
    MarkdownCleaningOutputValidator,
    validate_pipeline_output,
)
from app.features.markdown_cleaning.processors.errors import (
    MarkdownCleaningErrorCode,
    MarkdownCleaningProcessorError,
)
from app.features.markdown_cleaning.processors.models import (
    MarkdownCleaningContractVersion,
    MarkdownCleaningSummary,
    ProcessorResult,
)


def _make_summary() -> MarkdownCleaningSummary:
    return MarkdownCleaningSummary(
        duplicate_paragraphs_removed=0,
        phone_redactions=0,
        id_card_redactions=0,
        bank_card_redactions=0,
        email_redactions=0,
        ipv4_redactions=0,
        formatting_changes=0,
    )


def _make_result(
    output_file: Path,
    *,
    input_payload: bytes,
    output_payload: bytes | None = None,
    contract_version: MarkdownCleaningContractVersion = "markdown_cleaning_v1",
    expected_input_sha: str | None = None,
) -> ProcessorResult:
    if output_payload is None:
        output_payload = b"cleaned\n"
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_bytes(output_payload)
    return (
        ProcessorResult(
            output_path=output_file,
            input_sha256=hashlib.sha256(input_payload).hexdigest(),
            output_sha256=hashlib.sha256(output_payload).hexdigest(),
            contract_version=contract_version,
            summary=_make_summary(),
            input_bytes=len(input_payload),
            output_bytes=len(output_payload),
        )
        if expected_input_sha is None
        else ProcessorResult(
            output_path=output_file,
            input_sha256=expected_input_sha,
            output_sha256=hashlib.sha256(output_payload).hexdigest(),
            contract_version=contract_version,
            summary=_make_summary(),
            input_bytes=len(input_payload),
            output_bytes=len(output_payload),
        )
    )


def test_validate_pipeline_output_accepts_valid_result(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.txt"
    output_path = tmp_path / "result.md"
    payload = b"cleaned\n"
    source.write_bytes(b"input-content")

    validated = validate_pipeline_output(
        _make_result(output_path, input_payload=source.read_bytes()),
        expected_input_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        max_output_bytes=1024,
    )

    assert validated.output_path == output_path
    assert validated.output_bytes == len(payload)
    assert validated.output_sha256 == hashlib.sha256(payload).hexdigest()


def test_class_validate_pipeline_output_is_alias(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "result.md"
    source = tmp_path / "source.md"
    source.write_text("input", encoding="utf-8")

    validator = MarkdownCleaningOutputValidator()
    result = validator.validate(
        _make_result(output_path, input_payload=source.read_bytes()),
        expected_input_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        max_output_bytes=1024,
    )

    assert result.output_path == output_path


def test_validator_rejects_mismatched_contract_version(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.md"
    source.write_text("input", encoding="utf-8")
    result = _make_result(tmp_path / "result.md", input_payload=source.read_bytes())
    object.__setattr__(result, "contract_version", "legacy_contract")

    with pytest.raises(MarkdownCleaningProcessorError) as error:
        validate_pipeline_output(
            result,
            expected_input_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            max_output_bytes=1024,
        )

    assert error.value.code is MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT
    assert "协议版本不匹配" in error.value.safe_message


def test_validator_rejects_missing_expected_input_digest(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.md"
    source.write_text("input", encoding="utf-8")
    result = _make_result(tmp_path / "result.md", input_payload=source.read_bytes())

    with pytest.raises(MarkdownCleaningProcessorError) as error:
        validate_pipeline_output(
            result, expected_input_sha256="", max_output_bytes=1024
        )

    assert error.value.code is MarkdownCleaningErrorCode.INVALID_MARKDOWN_INPUT
    assert "缺少输入摘要" in error.value.safe_message


def test_validator_rejects_input_hash_mismatch(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.md"
    source.write_text("input", encoding="utf-8")
    result = _make_result(tmp_path / "result.md", input_payload=source.read_bytes())

    with pytest.raises(MarkdownCleaningProcessorError) as error:
        validate_pipeline_output(
            result,
            expected_input_sha256="0" * 64,
            max_output_bytes=1024,
        )

    assert error.value.code is MarkdownCleaningErrorCode.INVALID_MARKDOWN_INPUT
    assert "输入摘要与处理结果不一致" in error.value.safe_message


def test_validator_rejects_relative_output_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "source.md"
    source.write_text("input", encoding="utf-8")
    relative_path = Path("result.md")
    result = _make_result(
        relative_path,
        input_payload=source.read_bytes(),
        output_payload=b"cleaned",
    )

    with pytest.raises(MarkdownCleaningProcessorError) as error:
        validate_pipeline_output(
            result,
            expected_input_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            max_output_bytes=1024,
        )

    assert error.value.code is MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT
    assert "路径不安全" in error.value.safe_message
    assert "result.md" not in str(error.value)


def test_validator_rejects_non_markdown_output_extension(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.md"
    source.write_text("input", encoding="utf-8")
    result = _make_result(
        tmp_path / "result.txt",
        input_payload=source.read_bytes(),
        output_payload=b"cleaned",
    )

    with pytest.raises(MarkdownCleaningProcessorError) as error:
        validate_pipeline_output(
            result,
            expected_input_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            max_output_bytes=1024,
        )

    assert error.value.code is MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT
    assert "扩展名不正确" in error.value.safe_message


def test_validator_rejects_missing_output_file(tmp_path: Path) -> None:
    source = tmp_path / "source.md"
    source.write_text("input", encoding="utf-8")
    output_file = tmp_path / "missing.md"
    result = ProcessorResult(
        output_path=output_file,
        input_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        output_sha256=hashlib.sha256(b"cleaned").hexdigest(),
        contract_version="markdown_cleaning_v1",
        summary=_make_summary(),
        input_bytes=len(source.read_bytes()),
        output_bytes=7,
    )

    with pytest.raises(MarkdownCleaningProcessorError) as error:
        validate_pipeline_output(
            result,
            expected_input_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            max_output_bytes=1024,
        )

    assert error.value.code is MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT
    assert "不存在" in error.value.safe_message


def test_validator_rejects_output_read_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.md"
    source.write_text("input", encoding="utf-8")
    output_file = tmp_path / "result.md"
    output_file.write_bytes(b"cleaned")
    result = _make_result(output_file, input_payload=source.read_bytes())
    expected_input_sha = hashlib.sha256(source.read_bytes()).hexdigest()

    def _boom(*_args, **_kwargs) -> None:
        raise OSError("io error")

    monkeypatch.setattr(
        "app.features.markdown_cleaning.output_validator._open_path_nofollow",
        _boom,
    )
    with pytest.raises(MarkdownCleaningProcessorError) as error:
        validate_pipeline_output(
            result,
            expected_input_sha256=expected_input_sha,
            max_output_bytes=1024,
        )

    assert error.value.code is MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT
    assert "读取失败" in error.value.safe_message


def test_validate_output_bytes_limit_is_enforced(tmp_path: Path) -> None:
    source = tmp_path / "source.md"
    source.write_text("input", encoding="utf-8")
    result = _make_result(
        tmp_path / "result.md",
        input_payload=source.read_bytes(),
        output_payload=b"0123456789",
    )

    with pytest.raises(MarkdownCleaningProcessorError) as error:
        validate_pipeline_output(
            result,
            expected_input_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            max_output_bytes=5,
        )

    assert error.value.code is MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT
    assert "超出限制" in error.value.safe_message


def test_validator_rechecks_output_hash_and_length_from_actual_file(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.md"
    source.write_text("input", encoding="utf-8")
    output_file = tmp_path / "result.md"
    output_file.write_text("cleaned\n", encoding="utf-8")
    result = _make_result(output_file, input_payload=source.read_bytes())
    expected_input_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    output_file.write_text("tamperd!\n", encoding="utf-8")

    with pytest.raises(MarkdownCleaningProcessorError) as error:
        validate_pipeline_output(
            result,
            expected_input_sha256=expected_input_sha,
            max_output_bytes=1024,
        )

    assert error.value.code is MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT
    assert "摘要不一致" in error.value.safe_message


def test_output_validator_rejects_invalid_summary_fields(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.md"
    source.write_text("input", encoding="utf-8")
    result = _make_result(tmp_path / "result.md", input_payload=source.read_bytes())
    object.__setattr__(
        result,
        "summary",
        object.__new__(MarkdownCleaningSummary),
    )
    object.__setattr__(result.summary, "duplicate_paragraphs_removed", 1)
    object.__setattr__(result.summary, "phone_redactions", 2)
    object.__setattr__(result.summary, "id_card_redactions", -1)
    object.__setattr__(result.summary, "bank_card_redactions", 3)
    object.__setattr__(result.summary, "email_redactions", 4)
    object.__setattr__(result.summary, "ipv4_redactions", 5)
    object.__setattr__(result.summary, "formatting_changes", 6)

    with pytest.raises(MarkdownCleaningProcessorError) as error:
        validate_pipeline_output(
            result,
            expected_input_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            max_output_bytes=1024,
        )

    assert error.value.code is MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT
    assert "id_card_redactions" in error.value.safe_message


def test_validator_rejects_expected_output_path_mismatch(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.md"
    output = tmp_path / "result.md"
    source.write_text("input", encoding="utf-8")
    output.write_text("cleaned\n", encoding="utf-8")

    with pytest.raises(MarkdownCleaningProcessorError) as error:
        validate_pipeline_output(
            _make_result(
                output,
                input_payload=source.read_bytes(),
                output_payload=b"cleaned\n",
            ),
            expected_input_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            max_output_bytes=1024,
            expected_output_path=tmp_path / "other.md",
        )

    assert error.value.code is MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT
    assert "预期" in error.value.safe_message


def test_validator_rejects_source_path_digest_mismatch(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.md"
    result_source = tmp_path / "source_result.md"
    output = tmp_path / "result.md"
    source.write_text("input", encoding="utf-8")
    result_source.write_text("other", encoding="utf-8")
    output.write_text("cleaned\n", encoding="utf-8")

    with pytest.raises(MarkdownCleaningProcessorError) as error:
        validate_pipeline_output(
            _make_result(
                output,
                input_payload=source.read_bytes(),
                output_payload=b"cleaned\n",
            ),
            expected_input_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            max_output_bytes=1024,
            source_path=result_source,
        )

    assert error.value.code is MarkdownCleaningErrorCode.INVALID_MARKDOWN_INPUT


def test_validator_normalizes_missing_source_error(tmp_path: Path) -> None:
    output = tmp_path / "result.md"
    output.write_bytes(b"cleaned\n")

    with pytest.raises(MarkdownCleaningProcessorError) as error:
        validate_pipeline_output(
            _make_result(output, input_payload=b"input"),
            expected_input_sha256=hashlib.sha256(b"input").hexdigest(),
            max_output_bytes=1024,
            source_path=tmp_path / "missing.md",
        )

    assert error.value.code is MarkdownCleaningErrorCode.INVALID_MARKDOWN_INPUT
    assert str(tmp_path) not in str(error.value)


def test_validator_rejects_source_symlink_or_junction(tmp_path: Path) -> None:
    real = tmp_path / "real.md"
    linked = tmp_path / "linked.md"
    output = tmp_path / "result.md"
    real.write_bytes(b"input")
    output.write_bytes(b"cleaned\n")
    try:
        linked.symlink_to(real)
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")

    with pytest.raises(MarkdownCleaningProcessorError) as error:
        validate_pipeline_output(
            _make_result(output, input_payload=b"input"),
            expected_input_sha256=hashlib.sha256(b"input").hexdigest(),
            max_output_bytes=1024,
            source_path=linked,
        )

    assert error.value.code is MarkdownCleaningErrorCode.INVALID_MARKDOWN_INPUT
    assert "路径不安全" in error.value.safe_message


def test_validator_rejects_output_with_invalid_utf8(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.md"
    output = tmp_path / "result.md"
    source.write_text("input", encoding="utf-8")
    output.write_bytes(b"cleaned\xff\n")

    with pytest.raises(MarkdownCleaningProcessorError) as error:
        validate_pipeline_output(
            _make_result(
                output,
                input_payload=source.read_bytes(),
                output_payload=b"cleaned\xff\n",
            ),
            expected_input_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            max_output_bytes=1024,
        )

    assert error.value.code is MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT
    assert "UTF-8" in error.value.safe_message


def test_validator_rejects_output_with_bom(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.md"
    output = tmp_path / "result.md"
    source.write_text("input", encoding="utf-8")
    output.write_bytes(b"\xef\xbb\xbfcleaned\n")

    with pytest.raises(MarkdownCleaningProcessorError) as error:
        validate_pipeline_output(
            _make_result(
                output,
                input_payload=source.read_bytes(),
                output_payload=b"\xef\xbb\xbfcleaned\n",
            ),
            expected_input_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            max_output_bytes=1024,
        )

    assert error.value.code is MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT
    assert "BOM" in error.value.safe_message


def test_validator_rejects_output_with_crlf_newline(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.md"
    output = tmp_path / "result.md"
    source.write_text("input", encoding="utf-8")
    output.write_bytes(b"cleaned\r\n")

    with pytest.raises(MarkdownCleaningProcessorError) as error:
        validate_pipeline_output(
            _make_result(
                output,
                input_payload=source.read_bytes(),
                output_payload=b"cleaned\r\n",
            ),
            expected_input_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            max_output_bytes=1024,
        )

    assert error.value.code is MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT
    assert "换行" in error.value.safe_message


def test_validator_rejects_output_with_no_terminal_newline(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.md"
    output = tmp_path / "result.md"
    source.write_text("input", encoding="utf-8")
    output.write_text("cleaned", encoding="utf-8")

    with pytest.raises(MarkdownCleaningProcessorError) as error:
        validate_pipeline_output(
            _make_result(
                output,
                input_payload=source.read_bytes(),
                output_payload=b"cleaned",
            ),
            expected_input_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            max_output_bytes=1024,
        )

    assert error.value.code is MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT
    assert "换行约束" in error.value.safe_message


def test_validator_rejects_extra_terminal_newline(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.md"
    output = tmp_path / "result.md"
    source.write_text("input", encoding="utf-8")
    output.write_text("cleaned\n\n", encoding="utf-8")

    with pytest.raises(MarkdownCleaningProcessorError) as error:
        validate_pipeline_output(
            _make_result(
                output,
                input_payload=source.read_bytes(),
                output_payload=b"cleaned\n\n",
            ),
            expected_input_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            max_output_bytes=1024,
        )

    assert error.value.code is MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT
    assert "换行约束" in error.value.safe_message


def test_validator_rejects_output_matching_protected_baseline(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.md"
    output = tmp_path / "result.md"
    source.write_text("input", encoding="utf-8")
    output.write_text("cleaned\n", encoding="utf-8")
    protected_output_hash = hashlib.sha256(b"cleaned\n").hexdigest()

    with pytest.raises(MarkdownCleaningProcessorError) as error:
        validate_pipeline_output(
            _make_result(
                output,
                input_payload=source.read_bytes(),
                output_payload=b"cleaned\n",
            ),
            expected_input_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            max_output_bytes=1024,
            protected_baseline=protected_output_hash,
        )

    assert error.value.code is MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT
    assert "受保护基线" in error.value.safe_message


def test_validator_rejects_changed_protected_regions(tmp_path: Path) -> None:
    source = tmp_path / "source.md"
    output = tmp_path / "result.md"
    source.write_text("before `secret`\n\n```txt\nkeep\n```\n", encoding="utf-8")
    payload = b"before `changed`\n\n```txt\nkeep\n```\n"

    with pytest.raises(MarkdownCleaningProcessorError) as error:
        validate_pipeline_output(
            _make_result(
                output, input_payload=source.read_bytes(), output_payload=payload
            ),
            expected_input_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            max_output_bytes=1024,
            expected_output_path=output,
            source_path=source,
        )

    assert error.value.code is MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT
    assert "保护区" in error.value.safe_message


def test_validator_rejects_changed_protected_region_order_and_parent(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.md"
    output = tmp_path / "result.md"
    source.write_text("- `first`\n- `second`\n", encoding="utf-8")
    payload = b"`second`\n\n`first`\n"

    with pytest.raises(MarkdownCleaningProcessorError) as error:
        validate_pipeline_output(
            _make_result(
                output, input_payload=source.read_bytes(), output_payload=payload
            ),
            expected_input_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            max_output_bytes=1024,
            expected_output_path=output,
            source_path=source,
        )

    assert error.value.code is MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT
    assert "保护区" in error.value.safe_message


@pytest.mark.parametrize("token", ("__MD_INTERNAL_0__", "\ue000MD_CLEAN_0\ue001"))
def test_validator_rejects_internal_tokens(tmp_path: Path, token: str) -> None:
    source = tmp_path / "source.md"
    output = tmp_path / "result.md"
    source.write_text("input\n", encoding="utf-8")
    payload = f"{token}\n".encode()

    with pytest.raises(MarkdownCleaningProcessorError) as error:
        validate_pipeline_output(
            _make_result(
                output, input_payload=source.read_bytes(), output_payload=payload
            ),
            expected_input_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            max_output_bytes=1024,
            expected_output_path=output,
            source_path=source,
        )

    assert error.value.code is MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT
    assert "内部标记" in error.value.safe_message
