import hashlib
import os
import uuid
from pathlib import Path

import pytest

from app.features.markdown_cleaning.input_resolver import ResolvedMarkdownInput
from app.features.markdown_cleaning.input_validator import (
    MarkdownInputError,
    MarkdownInputErrorCode,
    MarkdownInputValidator,
)
from app.features.markdown_cleaning.staging import StagingLayout


def staged_input(
    tmp_path: Path,
    content: bytes,
    *,
    source_suffix: str = ".md",
) -> tuple[StagingLayout, ResolvedMarkdownInput]:
    layout = StagingLayout.for_task(tmp_path / "staging", uuid.uuid4())
    layout.prepare()
    layout.original_source.write_bytes(content)
    return layout, ResolvedMarkdownInput(
        path=layout.original_source,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        source_suffix=source_suffix,
    )


def test_bom_original_is_preserved_and_independent_no_bom_source_is_created(
    tmp_path: Path,
) -> None:
    content = (
        b"\xef\xbb\xbf# \xe6\xa0\x87\xe9\xa2\x98\r\n\r\n\xe6\xad\xa3\xe6\x96\x87\r\n"
    )
    layout, resolved = staged_input(tmp_path, content)

    validated = MarkdownInputValidator(max_input_bytes=1024).validate(resolved, layout)

    expected_processor = content[3:]
    assert layout.original_source.read_bytes() == content
    assert validated.original_path == layout.original_source
    assert validated.original_sha256 == hashlib.sha256(content).hexdigest()
    assert validated.original_size_bytes == len(content)
    assert validated.processor_path == layout.processor_source
    assert layout.processor_source.read_bytes() == expected_processor
    assert validated.processor_sha256 == hashlib.sha256(expected_processor).hexdigest()
    assert validated.processor_size_bytes == len(expected_processor)
    assert not layout.processor_source.samefile(layout.original_source)
    assert not list(layout.input_dir.glob("*.part"))


def test_no_bom_original_still_produces_independent_processor_file(
    tmp_path: Path,
) -> None:
    content = b"# title\r\n\r\nbody\r\n"
    layout, resolved = staged_input(tmp_path, content)

    validated = MarkdownInputValidator(max_input_bytes=1024).validate(resolved, layout)

    assert layout.original_source.read_bytes() == content
    assert layout.processor_source.read_bytes() == content
    assert validated.processor_sha256 == resolved.sha256
    assert layout.processor_source != layout.original_source
    assert not os.path.samefile(layout.processor_source, layout.original_source)


@pytest.mark.parametrize(
    ("content", "code"),
    [
        (b"", MarkdownInputErrorCode.EMPTY_INPUT),
        (b"\xff", MarkdownInputErrorCode.INVALID_UTF8),
        (b"before\xef\xbb\xbfafter", MarkdownInputErrorCode.INVALID_UTF8),
        (b"before\x00after", MarkdownInputErrorCode.INVALID_CONTROL_CHARACTER),
        (b"before\x01after", MarkdownInputErrorCode.INVALID_CONTROL_CHARACTER),
        (b"```python\nprint('x')\n", MarkdownInputErrorCode.INVALID_MARKDOWN),
        (b"~~~~\nnot closed by ~~~\n~~~\n", MarkdownInputErrorCode.INVALID_MARKDOWN),
    ],
)
def test_invalid_input_is_rejected_without_mutating_original_or_leaving_part(
    tmp_path: Path,
    content: bytes,
    code: MarkdownInputErrorCode,
) -> None:
    layout, resolved = staged_input(tmp_path, content)

    with pytest.raises(MarkdownInputError) as captured:
        MarkdownInputValidator(max_input_bytes=1024).validate(resolved, layout)

    assert captured.value.code is code
    assert layout.original_source.read_bytes() == content
    assert not layout.processor_source.exists()
    assert not list(layout.input_dir.glob("*.part"))


def test_validator_rechecks_actual_size_and_hash_before_use(tmp_path: Path) -> None:
    layout, resolved = staged_input(tmp_path, b"original")
    layout.original_source.write_bytes(b"tampered")

    with pytest.raises(MarkdownInputError) as captured:
        MarkdownInputValidator(max_input_bytes=1024).validate(resolved, layout)

    assert captured.value.code is MarkdownInputErrorCode.INPUT_DIGEST_MISMATCH
    assert not layout.processor_source.exists()


def test_validator_enforces_size_even_if_resolver_metadata_claims_smaller(
    tmp_path: Path,
) -> None:
    layout, resolved = staged_input(tmp_path, b"12345")

    with pytest.raises(MarkdownInputError) as captured:
        MarkdownInputValidator(max_input_bytes=4).validate(resolved, layout)

    assert captured.value.code is MarkdownInputErrorCode.INPUT_TOO_LARGE
    assert not layout.processor_source.exists()


def test_closed_fences_with_longer_closer_are_accepted(tmp_path: Path) -> None:
    content = b"```python\nprint('x')\n````\n\n~~~\nvalue\n~~~\n"
    layout, resolved = staged_input(tmp_path, content)

    validated = MarkdownInputValidator(max_input_bytes=1024).validate(resolved, layout)

    assert validated.processor_path.read_bytes() == content


def test_existing_processor_source_is_reused_only_when_hash_matches(
    tmp_path: Path,
) -> None:
    content = b"# title\n"
    layout, resolved = staged_input(tmp_path, content)
    validator = MarkdownInputValidator(max_input_bytes=1024)
    first = validator.validate(resolved, layout)
    first_stat = layout.processor_source.stat()

    second = validator.validate(
        resolved,
        layout,
        expected_processor_sha256=first.processor_sha256,
        expected_processor_size_bytes=first.processor_size_bytes,
    )

    assert second == first
    assert layout.processor_source.stat().st_mtime_ns == first_stat.st_mtime_ns


def test_stale_processor_source_is_atomically_replaced(tmp_path: Path) -> None:
    content = b"# title\n"
    layout, resolved = staged_input(tmp_path, content)
    layout.processor_source.write_bytes(b"tampered")

    validated = MarkdownInputValidator(max_input_bytes=1024).validate(
        resolved,
        layout,
        expected_processor_sha256=hashlib.sha256(content).hexdigest(),
        expected_processor_size_bytes=len(content),
    )

    assert layout.processor_source.read_bytes() == content
    assert validated.processor_sha256 == hashlib.sha256(content).hexdigest()
