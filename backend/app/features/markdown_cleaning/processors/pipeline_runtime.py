"""Killable subprocess runtime for Markdown cleaning transformations."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import cast

from app.features.markdown_cleaning.processors.errors import (
    MarkdownCleaningProcessorError,
    map_processing_exception,
)
from app.features.markdown_cleaning.processors.pipeline import (
    MarkdownCleaningPipeline,
    MarkdownCleaningPipelineLimits,
    _ProcessingDeadline,
)


def _decode_request(raw_request: bytes) -> tuple[str, MarkdownCleaningPipelineLimits]:
    parsed = json.loads(raw_request.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("runtime request must be an object")
    request = cast(dict[str, object], parsed)
    markdown = request.get("markdown")
    raw_limits = request.get("limits")
    if not isinstance(markdown, str) or not isinstance(raw_limits, dict):
        raise ValueError("runtime request fields are invalid")
    limits = cast(dict[str, object], raw_limits)
    return markdown, MarkdownCleaningPipelineLimits(
        max_input_bytes=int(cast(int, limits["max_input_bytes"])),
        max_output_bytes=int(cast(int, limits["max_output_bytes"])),
        max_block_count=int(cast(int, limits["max_block_count"])),
        max_protected_span_count=int(cast(int, limits["max_protected_span_count"])),
        max_block_char_span=int(cast(int, limits["max_block_char_span"])),
        max_token_count=int(cast(int, limits["max_token_count"])),
        max_pii_candidate_count=int(cast(int, limits["max_pii_candidate_count"])),
        processing_timeout_seconds=float(
            cast(float, limits["processing_timeout_seconds"])
        ),
    )


def _execute(raw_request: bytes) -> dict[str, object]:
    markdown, limits = _decode_request(raw_request)
    pipeline = MarkdownCleaningPipeline(
        staging_root=Path.cwd(),
        limits=limits,
        _run_inline=True,
    )
    deadline = _ProcessingDeadline(
        started_at=time.perf_counter(),
        timeout_seconds=limits.processing_timeout_seconds,
        time_fn=time.perf_counter,
    )
    transformed = pipeline._transform_text(markdown, deadline)
    redaction = transformed.redaction_summary
    return {
        "ok": True,
        "text": transformed.text,
        "duplicateCount": transformed.duplicate_count,
        "redactionSummary": {
            "phone": redaction.phone,
            "idCard": redaction.id_card,
            "bankCard": redaction.bank_card,
            "email": redaction.email,
            "ipv4": redaction.ipv4,
        },
        "formattingChanges": transformed.formatting_changes,
    }


def main() -> int:
    try:
        response = _execute(sys.stdin.buffer.read())
    except MarkdownCleaningProcessorError as exc:
        response = {
            "ok": False,
            "errorCode": exc.code.value,
            "safeMessage": exc.safe_message,
        }
    except Exception as exc:
        mapped = map_processing_exception(exc)
        response = {
            "ok": False,
            "errorCode": mapped.code.value,
            "safeMessage": mapped.safe_message,
        }
    sys.stdout.buffer.write(
        json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
