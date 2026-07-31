import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from datajuicer_service.profiles.models import InputSample


class ProfileInputError(ValueError):
    def __init__(self, code: str, *, line_number: int | None = None) -> None:
        self.code = code
        self.line_number = line_number
        location = "" if line_number is None else f" at line {line_number}"
        super().__init__(f"{code}{location}")


@dataclass(frozen=True, slots=True)
class InputLimits:
    max_records: int
    max_bytes: int
    max_text_chars: int

    def __post_init__(self) -> None:
        for name in ("max_records", "max_bytes", "max_text_chars"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")


def _parse_record(raw_line: bytes, line_number: int) -> dict[str, Any]:
    try:
        line = raw_line.decode("utf-8").rstrip("\r\n")
    except UnicodeDecodeError as error:
        raise ProfileInputError("INVALID_UTF8", line_number=line_number) from error
    if not line:
        raise ProfileInputError("BLANK_LINE", line_number=line_number)
    try:
        record = json.loads(line)
    except json.JSONDecodeError as error:
        raise ProfileInputError("INVALID_JSON", line_number=line_number) from error
    if not isinstance(record, dict):
        raise ProfileInputError("INVALID_RECORD", line_number=line_number)
    if set(record) != {"uid", "text"}:
        raise ProfileInputError("UNKNOWN_FIELDS", line_number=line_number)
    return record


def load_input_jsonl(path: Path, limits: InputLimits) -> list[InputSample]:
    samples: list[InputSample] = []
    seen_uids: set[int] = set()
    total_bytes = 0
    total_text_chars = 0

    try:
        with path.open("rb") as stream:
            line_number = 0
            while True:
                remaining_bytes = limits.max_bytes - total_bytes
                raw_line = stream.readline(remaining_bytes + 1)
                if not raw_line:
                    break
                total_bytes += len(raw_line)
                if total_bytes > limits.max_bytes:
                    raise ProfileInputError("MAX_BYTES")

                line_number += 1
                if line_number > limits.max_records:
                    raise ProfileInputError("MAX_RECORDS")
                record = _parse_record(raw_line, line_number)

                uid = record["uid"]
                if isinstance(uid, bool) or not isinstance(uid, int) or uid < 0:
                    raise ProfileInputError("INVALID_UID", line_number=line_number)
                if uid in seen_uids:
                    raise ProfileInputError("DUPLICATE_UID", line_number=line_number)

                text = record["text"]
                if not isinstance(text, str):
                    raise ProfileInputError("INVALID_TEXT", line_number=line_number)
                total_text_chars += len(text)
                if total_text_chars > limits.max_text_chars:
                    raise ProfileInputError("MAX_TEXT_CHARS")

                seen_uids.add(uid)
                samples.append(InputSample(uid=uid, text=text))
    except OSError as error:
        raise ProfileInputError("INPUT_UNREADABLE") from error

    if not samples:
        raise ProfileInputError("EMPTY_INPUT")
    return samples
