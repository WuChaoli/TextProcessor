import json
import uuid
from pathlib import Path

from app.features.global_deduplication.models import (
    DocumentReference,
    NormalizedDocument,
)
from app.features.global_deduplication.staging import (
    GlobalDeduplicationStaging,
    GlobalDeduplicationStagingLayout,
)


def test_staging_path_is_derived_only_from_task_id(tmp_path: Path) -> None:
    task_id = uuid.uuid7()

    layout = GlobalDeduplicationStagingLayout.for_task(tmp_path, task_id)

    assert layout.root == tmp_path.resolve(strict=False) / str(task_id)
    assert layout.input_jsonl.parent == layout.root
    assert layout.mapping_json.parent == layout.root


def test_staging_separates_text_from_business_mapping(tmp_path: Path) -> None:
    task_id = uuid.uuid7()
    documents = (
        NormalizedDocument(
            reference=DocumentReference(
                file_id="business-1",
                file_storage_path="/data/source/one.md",
            ),
            text="正文一",
            size_bytes=9,
        ),
        NormalizedDocument(
            reference=DocumentReference(
                file_id="business-2",
                file_storage_path="/data/source/two.json",
            ),
            text='{"raw": true}',
            size_bytes=13,
        ),
    )

    prepared = GlobalDeduplicationStaging(tmp_path).prepare(task_id, documents)

    input_records = [
        json.loads(line)
        for line in prepared.layout.input_jsonl.read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    mapping = json.loads(prepared.layout.mapping_json.read_text(encoding="utf-8"))
    manifest = json.loads(prepared.layout.manifest.read_text(encoding="utf-8"))
    assert input_records == [
        {"uid": 0, "text": "正文一"},
        {"uid": 1, "text": '{"raw": true}'},
    ]
    assert "business-1" not in prepared.layout.input_jsonl.read_text(encoding="utf-8")
    assert mapping == {
        "schemaVersion": 1,
        "taskId": str(task_id),
        "documents": [
            {
                "uid": 0,
                "fileId": "business-1",
                "fileStoragePath": "/data/source/one.md",
            },
            {
                "uid": 1,
                "fileId": "business-2",
                "fileStoragePath": "/data/source/two.json",
            },
        ],
    }
    assert "正文一" not in prepared.layout.mapping_json.read_text(encoding="utf-8")
    assert manifest["inputDocumentCount"] == 2
    assert manifest["inputJsonlSha256"] == prepared.input_jsonl_sha256
    assert manifest["mappingSha256"] == prepared.mapping_sha256


def test_staging_reuses_complete_matching_files(tmp_path: Path) -> None:
    task_id = uuid.uuid7()
    documents = (
        NormalizedDocument(
            reference=DocumentReference(file_id="1", file_storage_path="a.txt"),
            text="same",
            size_bytes=4,
        ),
    )
    staging = GlobalDeduplicationStaging(tmp_path)

    first = staging.prepare(task_id, documents)
    second = staging.prepare(task_id, documents)

    assert second == first
