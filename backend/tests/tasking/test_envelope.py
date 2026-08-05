from uuid import uuid4

import pytest

from app.tasking.envelope import TaskEnvelope


def test_envelope_round_trip_uses_snake_case() -> None:
    task_id = uuid4()

    payload = TaskEnvelope(task_id, "structured_extraction", 1).as_payload()
    parsed = TaskEnvelope.parse(
        payload,
        expected_type="structured_extraction",
        expected_schema_version=1,
    )

    assert payload == {
        "task_id": str(task_id),
        "task_type": "structured_extraction",
        "schema_version": 1,
    }
    assert parsed.task_id == task_id


def test_envelope_accepts_exact_legacy_camel_case_payload() -> None:
    task_id = uuid4()

    parsed = TaskEnvelope.parse(
        {
            "taskId": str(task_id),
            "taskType": "markdown_cleaning",
            "schemaVersion": 1,
        },
        expected_type="markdown_cleaning",
        expected_schema_version=1,
    )

    assert parsed.task_id == task_id


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"task_id": "bad", "task_type": "x", "schema_version": 1},
        {"task_id": str(uuid4()), "task_type": "x", "schema_version": True},
        {
            "task_id": str(uuid4()),
            "task_type": "x",
            "schema_version": 1,
            "extra": "forbidden",
        },
    ],
)
def test_envelope_rejects_invalid_payload(payload: object) -> None:
    with pytest.raises(ValueError, match="INVALID_TASK_ENVELOPE"):
        TaskEnvelope.parse(payload, expected_type="x", expected_schema_version=1)

