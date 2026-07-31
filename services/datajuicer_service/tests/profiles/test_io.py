from pathlib import Path

import pytest

from datajuicer_service.profiles.io import (
    InputLimits,
    ProfileInputError,
    load_input_jsonl,
)
from datajuicer_service.profiles.models import InputSample

LIMITS = InputLimits(max_records=10, max_bytes=1024, max_text_chars=1024)


def write_bytes(path: Path, content: bytes) -> Path:
    path.write_bytes(content)
    return path


def test_load_input_streams_valid_jsonl_in_file_order(tmp_path: Path) -> None:
    path = write_bytes(
        tmp_path / "input.jsonl",
        '{"uid":7,"text":"第一篇"}\n{"uid":2,"text":"second"}\n'.encode(),
    )

    assert load_input_jsonl(path, LIMITS) == [
        InputSample(uid=7, text="第一篇"),
        InputSample(uid=2, text="second"),
    ]


def test_load_input_rejects_duplicate_uid(tmp_path: Path) -> None:
    path = write_bytes(
        tmp_path / "input.jsonl",
        b'{"uid":0,"text":"a"}\n{"uid":0,"text":"b"}\n',
    )

    with pytest.raises(ProfileInputError, match="DUPLICATE_UID"):
        load_input_jsonl(path, LIMITS)


@pytest.mark.parametrize(
    ("content", "error_code"),
    [
        (b"", "EMPTY_INPUT"),
        (b"\n", "BLANK_LINE"),
        (b'{"uid":0,"text":"a","extra":1}\n', "UNKNOWN_FIELDS"),
        (b'{"uid":0,"text":"a"\n', "INVALID_JSON"),
        (b'["not","an","object"]\n', "INVALID_RECORD"),
        (b'{"uid":true,"text":"a"}\n', "INVALID_UID"),
        (b'{"uid":-1,"text":"a"}\n', "INVALID_UID"),
        (b'{"uid":0,"text":1}\n', "INVALID_TEXT"),
        (b"\xff\n", "INVALID_UTF8"),
    ],
)
def test_load_input_rejects_invalid_dataset(
    tmp_path: Path,
    content: bytes,
    error_code: str,
) -> None:
    path = write_bytes(tmp_path / "input.jsonl", content)

    with pytest.raises(ProfileInputError, match=error_code):
        load_input_jsonl(path, LIMITS)


@pytest.mark.parametrize(
    ("limits", "error_code"),
    [
        (InputLimits(max_records=1, max_bytes=1024, max_text_chars=1024), "MAX_RECORDS"),
        (InputLimits(max_records=10, max_bytes=20, max_text_chars=1024), "MAX_BYTES"),
        (InputLimits(max_records=10, max_bytes=1024, max_text_chars=1), "MAX_TEXT_CHARS"),
    ],
)
def test_load_input_enforces_configured_limits(
    tmp_path: Path,
    limits: InputLimits,
    error_code: str,
) -> None:
    path = write_bytes(
        tmp_path / "input.jsonl",
        b'{"uid":0,"text":"aa"}\n{"uid":1,"text":"b"}\n',
    )

    with pytest.raises(ProfileInputError, match=error_code):
        load_input_jsonl(path, limits)


def test_input_limits_reject_non_positive_values() -> None:
    with pytest.raises(ValueError, match="max_records"):
        InputLimits(max_records=0, max_bytes=1, max_text_chars=1)
