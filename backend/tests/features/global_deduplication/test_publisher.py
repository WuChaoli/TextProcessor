import json
from pathlib import Path

import pytest

from app.features.global_deduplication.errors import (
    GlobalDeduplicationErrorCode,
    GlobalDeduplicationProcessingError,
)
from app.features.global_deduplication.publisher import FinalResultPublisher
from app.features.global_deduplication.result_mapper import BusinessResult


def results() -> tuple[BusinessResult, ...]:
    return (
        BusinessResult(
            file_id="1",
            file_storage_path="/data/1.md",
            group_id=None,
            keep=True,
        ),
    )


def test_publisher_writes_clean_json_and_never_overwrites(tmp_path: Path) -> None:
    staging = tmp_path / "staging" / "final-result.json"
    target = tmp_path / "output" / "result.json"
    publisher = FinalResultPublisher()

    prepared = publisher.prepare(results(), staging)
    published = publisher.publish(prepared, target, allow_recovery=False)

    assert published.sha256 == prepared.sha256
    assert json.loads(target.read_text(encoding="utf-8")) == [
        {
            "fileId": "1",
            "fileStoragePath": "/data/1.md",
            "groupId": None,
            "keep": True,
        }
    ]
    with pytest.raises(GlobalDeduplicationProcessingError) as error:
        publisher.publish(prepared, target, allow_recovery=False)
    assert error.value.code is GlobalDeduplicationErrorCode.OUTPUT_CONFLICT


def test_publisher_recovers_only_matching_existing_output(tmp_path: Path) -> None:
    staging = tmp_path / "staging" / "final-result.json"
    target = tmp_path / "output" / "result.json"
    publisher = FinalResultPublisher()
    prepared = publisher.prepare(results(), staging)
    target.parent.mkdir(parents=True)
    target.write_bytes(staging.read_bytes())

    recovered = publisher.publish(prepared, target, allow_recovery=True)

    assert recovered.sha256 == prepared.sha256
    target.write_text("different", encoding="utf-8")
    with pytest.raises(GlobalDeduplicationProcessingError) as error:
        publisher.publish(prepared, target, allow_recovery=True)
    assert error.value.code is GlobalDeduplicationErrorCode.OUTPUT_CONFLICT
