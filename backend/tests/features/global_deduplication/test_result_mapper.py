import hashlib
import json
import uuid
from pathlib import Path

import pytest

from app.features.global_deduplication.errors import (
    GlobalDeduplicationErrorCode,
    GlobalDeduplicationProcessingError,
)
from app.features.global_deduplication.result_mapper import (
    MappingDocument,
    map_business_result,
    validate_processor_output,
)


def write_result(path: Path, records: list[dict[str, object]]) -> str:
    content = "".join(
        json.dumps(record, separators=(",", ":")) + "\n" for record in records
    ).encode()
    path.write_bytes(content)
    return hashlib.sha256(content).hexdigest()


def test_group_id_and_keep_are_stable_under_result_reordering(
    tmp_path: Path,
) -> None:
    task_id = uuid.uuid7()
    path = tmp_path / "result.jsonl"
    records = [
        {
            "uid": 2,
            "clusterId": None,
            "representative": True,
            "method": None,
        },
        {
            "uid": 1,
            "clusterId": "external-secret",
            "representative": False,
            "method": "exact_minhash",
        },
        {
            "uid": 0,
            "clusterId": "external-secret",
            "representative": True,
            "method": "exact_minhash",
        },
    ]
    digest = write_result(path, records)
    decisions = validate_processor_output(
        path,
        expected_uids={0, 1, 2},
        expected_sha256=digest,
    )
    mapping = (
        MappingDocument(0, "f0", "/data/0.md"),
        MappingDocument(1, "f1", "/data/1.txt"),
        MappingDocument(2, "f2", "/data/2.json"),
    )

    first = map_business_result(task_id, mapping, decisions)
    repeated = map_business_result(task_id, mapping, tuple(reversed(decisions)))

    assert first == repeated
    assert first[0].group_id == first[1].group_id
    assert first[0].group_id != "external-secret"
    assert first[0].keep is True
    assert first[1].keep is False
    assert first[2].group_id is None
    assert first[2].keep is True
    assert [set(item.to_public_dict()) for item in first] == [
        {"fileId", "fileStoragePath", "groupId", "keep"},
        {"fileId", "fileStoragePath", "groupId", "keep"},
        {"fileId", "fileStoragePath", "groupId", "keep"},
    ]


@pytest.mark.parametrize(
    "records",
    [
        [
            {
                "uid": 0,
                "clusterId": "c",
                "representative": True,
                "method": "exact",
            }
        ],
        [
            {
                "uid": 0,
                "clusterId": None,
                "representative": False,
                "method": None,
            }
        ],
        [
            {
                "uid": 0,
                "clusterId": "c",
                "representative": True,
                "method": "exact",
            },
            {
                "uid": 1,
                "clusterId": "c",
                "representative": True,
                "method": "exact",
            },
        ],
        [
            {
                "uid": 0,
                "clusterId": "c",
                "representative": True,
                "method": "exact",
                "text": "leak",
            },
            {
                "uid": 1,
                "clusterId": "c",
                "representative": False,
                "method": "exact",
            },
        ],
    ],
)
def test_processor_output_rejects_cluster_invariant_violations(
    tmp_path: Path,
    records: list[dict[str, object]],
) -> None:
    path = tmp_path / "result.jsonl"
    digest = write_result(path, records)

    with pytest.raises(GlobalDeduplicationProcessingError) as error:
        validate_processor_output(
            path,
            expected_uids=set(range(len(records))),
            expected_sha256=digest,
        )

    assert error.value.code is GlobalDeduplicationErrorCode.INVALID_PROCESSOR_OUTPUT


def test_processor_output_rejects_digest_and_uid_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "result.jsonl"
    digest = write_result(
        path,
        [
            {
                "uid": 0,
                "clusterId": None,
                "representative": True,
                "method": None,
            }
        ],
    )

    with pytest.raises(GlobalDeduplicationProcessingError):
        validate_processor_output(
            path,
            expected_uids={0},
            expected_sha256="0" * 64,
        )
    with pytest.raises(GlobalDeduplicationProcessingError):
        validate_processor_output(
            path,
            expected_uids={0, 1},
            expected_sha256=digest,
        )
