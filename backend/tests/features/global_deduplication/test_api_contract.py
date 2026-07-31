from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.features.global_deduplication.schemas import (
    GlobalDeduplicationTaskCreate,
    GlobalDeduplicationTaskPublic,
)
from app.features.global_deduplication.state_machine import (
    GlobalDeduplicationTaskStatus,
)


def test_create_schema_uses_camel_case_and_rejects_blank_values() -> None:
    request = GlobalDeduplicationTaskCreate.model_validate(
        {
            "sessionId": " session-1 ",
            "inputJsonPath": " C:/input/manifest.json ",
            "targetPath": " C:/output/result.json ",
        }
    )

    assert request.session_id == "session-1"
    assert request.model_dump(by_alias=True) == {
        "sessionId": "session-1",
        "inputJsonPath": "C:/input/manifest.json",
        "targetPath": "C:/output/result.json",
    }
    with pytest.raises(ValidationError):
        GlobalDeduplicationTaskCreate.model_validate(
            {
                "sessionId": " ",
                "inputJsonPath": "C:/input/manifest.json",
                "targetPath": "C:/output/result.json",
            }
        )
    with pytest.raises(ValidationError):
        GlobalDeduplicationTaskCreate.model_validate(
            {
                "sessionId": "session-1",
                "inputJsonPath": "C:/input/manifest.json",
                "targetPath": "C:/output/result.md",
            }
        )


def test_public_task_result_and_error_are_mutually_exclusive() -> None:
    with pytest.raises(ValidationError):
        GlobalDeduplicationTaskPublic.model_validate(
            {
                "taskId": "019fb000-0000-7000-8000-000000000001",
                "sessionId": "session-1",
                "status": GlobalDeduplicationTaskStatus.SUCCEEDED,
                "createdAt": datetime.now(UTC),
                "startedAt": datetime.now(UTC),
                "finishedAt": datetime.now(UTC),
                "progress": {
                    "phase": "completed",
                    "total": 1,
                    "processed": 1,
                    "percent": 100,
                },
                "result": {"targetPath": "C:/output/result.json"},
                "error": {"code": "FAIL", "message": "failed"},
            }
        )
