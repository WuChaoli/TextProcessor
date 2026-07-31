import uuid

import pytest

from app.features.global_deduplication.messages import (
    GlobalDeduplicationMessage,
    InvalidGlobalDeduplicationMessage,
)


def test_message_round_trips_minimal_payload() -> None:
    task_id = uuid.uuid7()
    payload = {
        "taskId": str(task_id),
        "taskType": "global_deduplication",
        "schemaVersion": 1,
    }

    message = GlobalDeduplicationMessage.parse(payload)

    assert message.task_id == task_id
    assert message.as_payload() == payload


@pytest.mark.parametrize(
    "payload",
    [
        {
            "taskId": str(uuid.uuid7()),
            "taskType": "other",
            "schemaVersion": 1,
        },
        {
            "taskId": str(uuid.uuid7()),
            "taskType": "global_deduplication",
            "schemaVersion": 2,
        },
        {
            "taskId": str(uuid.uuid7()),
            "taskType": "global_deduplication",
            "schemaVersion": 1,
            "inputJsonPath": "/data/input.json",
        },
        {
            "taskId": "not-a-uuid",
            "taskType": "global_deduplication",
            "schemaVersion": 1,
        },
    ],
)
def test_message_rejects_non_contract_payload(payload: dict[str, object]) -> None:
    with pytest.raises(InvalidGlobalDeduplicationMessage):
        GlobalDeduplicationMessage.parse(payload)
