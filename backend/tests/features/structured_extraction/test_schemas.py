from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.features.structured_extraction.schemas import (
    ExtractionErrorPublic,
    ExtractionResultPublic,
    ExtractionTaskCreate,
    ExtractionTaskPublic,
)


def test_create_requires_one_input() -> None:
    with pytest.raises(ValidationError):
        ExtractionTaskCreate(
            sessionId="s-1",
            fileId="11",
            targetPath="/data/output/1.md",
        )


def test_create_rejects_blank_identifiers() -> None:
    with pytest.raises(ValidationError):
        ExtractionTaskCreate(
            sessionId=" ",
            fileId="11",
            fileStoragePath="/data/input/1.txt",
            targetPath="/data/output/1.md",
        )


def test_create_prefers_local_without_dropping_original_fields() -> None:
    request = ExtractionTaskCreate(
        sessionId="s-1",
        fileId="11",
        fileStoragePath="/data/input/1.txt",
        fileOssUrl="http://files.internal/1.txt",
        targetPath="/data/output/1.md",
    )

    assert request.selected_input_type == "local"
    payload = request.model_dump(by_alias=True)
    assert payload["fileStoragePath"] == "/data/input/1.txt"
    assert payload["fileOssUrl"] == "http://files.internal/1.txt"


def test_public_task_rejects_result_and_error_together() -> None:
    now = datetime.now(UTC)

    with pytest.raises(ValidationError):
        ExtractionTaskPublic(
            taskId="018f0000-0000-7000-8000-000000000001",
            sessionId="s-1",
            fileId="11",
            status="failed",
            createdAt=now,
            result=ExtractionResultPublic(
                fileStoragePath="/data/input/1.txt",
                fileOssUrl=None,
                targetPath="/data/output/1.md",
            ),
            error=ExtractionErrorPublic(
                code="OUTPUT_CONFLICT",
                message="目标文件已存在",
            ),
        )


def test_public_task_serializes_camel_case() -> None:
    now = datetime.now(UTC)
    task = ExtractionTaskPublic(
        taskId="018f0000-0000-7000-8000-000000000001",
        sessionId="s-1",
        fileId="11",
        status="queued",
        createdAt=now,
    )

    assert set(task.model_dump(by_alias=True)) == {
        "taskId",
        "sessionId",
        "fileId",
        "status",
        "createdAt",
        "startedAt",
        "finishedAt",
        "result",
        "error",
    }
