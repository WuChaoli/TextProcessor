from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.features.markdown_cleaning.api_errors import (
    MarkdownCleaningApiErrorCode,
    MarkdownCleaningDomainError,
)
from app.features.markdown_cleaning.schemas import (
    MarkdownCleaningSummaryPublic,
    MarkdownCleaningTaskCreate,
    MarkdownCleaningTaskPublic,
)
from app.features.markdown_cleaning.state_machine import MarkdownCleaningTaskStatus


def test_create_schema_selects_local_input_and_rejects_unknown_fields() -> None:
    request = MarkdownCleaningTaskCreate.model_validate(
        {
            "sessionId": " session-1 ",
            "fileId": " 11 ",
            "fileStoragePath": " C:/input/source.md ",
            "fileOssUrl": "https://files.internal/source.md",
            "targetPath": " C:/output/result.md ",
        }
    )

    assert request.session_id == "session-1"
    assert request.file_id == "11"
    assert request.selected_input_type == "local"
    with pytest.raises(ValidationError):
        MarkdownCleaningTaskCreate.model_validate(
            {
                **request.model_dump(by_alias=True),
                "processor": "caller-controlled",
            }
        )


@pytest.mark.parametrize(
    "field",
    ["sessionId", "fileId", "fileStoragePath", "fileOssUrl", "targetPath"],
)
def test_create_schema_rejects_blank_values(field: str) -> None:
    payload: dict[str, str | None] = {
        "sessionId": "session-1",
        "fileId": "11",
        "fileStoragePath": "C:/input/source.md",
        "fileOssUrl": "https://files.internal/source.md",
        "targetPath": "C:/output/result.md",
    }
    payload[field] = " "

    with pytest.raises(ValidationError):
        MarkdownCleaningTaskCreate.model_validate(payload)


@pytest.mark.parametrize(
    "input_field",
    ["fileStoragePath", "fileOssUrl"],
)
def test_create_schema_requires_at_least_one_input(input_field: str) -> None:
    payload: dict[str, str | None] = {
        "sessionId": "session-1",
        "fileId": "11",
        "fileStoragePath": "C:/input/source.md",
        "fileOssUrl": "https://files.internal/source.md",
        "targetPath": "C:/output/result.md",
    }
    payload[input_field] = None

    request = MarkdownCleaningTaskCreate.model_validate(payload)
    assert request.selected_input_type == (
        "remote" if input_field == "fileStoragePath" else "local"
    )

    payload["fileStoragePath"] = None
    payload["fileOssUrl"] = None
    with pytest.raises(ValidationError):
        MarkdownCleaningTaskCreate.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("fileStoragePath", "C:/input/source.txt"),
        ("fileOssUrl", "https://files.internal/source.html"),
        ("targetPath", "C:/output/result.rst"),
    ],
)
def test_create_schema_requires_case_insensitive_markdown_suffix(
    field: str, value: str
) -> None:
    payload = {
        "sessionId": "session-1",
        "fileId": "11",
        "fileStoragePath": "C:/input/source.MD",
        "fileOssUrl": "https://files.internal/source.MD",
        "targetPath": "C:/output/result.MD",
    }
    payload[field] = value

    with pytest.raises(ValidationError):
        MarkdownCleaningTaskCreate.model_validate(payload)


def test_create_schema_accepts_markdown_suffix_case_insensitively() -> None:
    request = MarkdownCleaningTaskCreate.model_validate(
        {
            "sessionId": "session-1",
            "fileId": "11",
            "fileStoragePath": "C:/input/source.MARKDOWN",
            "fileOssUrl": "https://files.internal/source.markdown",
            "targetPath": "C:/output/result.MarkDown",
        }
    )

    assert request.file_storage_path == "C:/input/source.MARKDOWN"
    assert request.file_oss_url == "https://files.internal/source.markdown"
    assert request.target_path == "C:/output/result.MarkDown"


def test_task_public_requires_a_result_only_for_success_and_never_an_error() -> None:
    payload = {
        "taskId": "019fb000-0000-7000-8000-000000000001",
        "sessionId": "session-1",
        "fileId": "11",
        "status": MarkdownCleaningTaskStatus.SUCCEEDED,
        "createdAt": datetime.now(UTC),
        "startedAt": datetime.now(UTC),
        "finishedAt": datetime.now(UTC),
        "progress": {"phase": "completed", "percent": 100},
        "result": {
            "fileId": "11",
            "fileStoragePath": "C:/output/source.md",
            "fileOssUrl": "https://oss.internal/source.md",
            "targetPath": "C:/output/result.md",
            "summary": {
                "duplicateParagraphsRemoved": 2,
                "redactions": {
                    "phone": 1,
                    "idCard": 0,
                    "bankCard": 0,
                    "email": 1,
                    "ipv4": 0,
                },
                "formattingChanges": 3,
            },
        },
        "error": {"code": "PROCESSING_FAILED", "message": "failed"},
    }

    with pytest.raises(ValidationError):
        MarkdownCleaningTaskPublic.model_validate(payload)

    payload["error"] = None
    assert MarkdownCleaningTaskPublic.model_validate(payload).result is not None

    payload["status"] = MarkdownCleaningTaskStatus.FAILED
    with pytest.raises(ValidationError):
        MarkdownCleaningTaskPublic.model_validate(payload)


def test_summary_is_count_only_and_rejects_unknown_fields() -> None:
    summary = MarkdownCleaningSummaryPublic.model_validate(
        {
            "duplicateParagraphsRemoved": 2,
            "redactions": {
                "phone": 1,
                "idCard": 0,
                "bankCard": 0,
                "email": 1,
                "ipv4": 0,
            },
            "formattingChanges": 3,
        }
    )

    assert summary.model_dump(by_alias=True) == {
        "duplicateParagraphsRemoved": 2,
        "redactions": {
            "phone": 1,
            "idCard": 0,
            "bankCard": 0,
            "email": 1,
            "ipv4": 0,
        },
        "formattingChanges": 3,
    }
    with pytest.raises(ValidationError):
        MarkdownCleaningSummaryPublic.model_validate(
            {
                **summary.model_dump(by_alias=True),
                "cleanedContent": "sensitive content must not be public",
            }
        )


def test_summary_exposes_exactly_five_camel_case_redaction_counts() -> None:
    summary = MarkdownCleaningSummaryPublic.model_validate(
        {
            "duplicateParagraphsRemoved": 2,
            "redactions": {
                "phone": 1,
                "idCard": 2,
                "bankCard": 3,
                "email": 4,
                "ipv4": 5,
            },
            "formattingChanges": 3,
        }
    )

    assert summary.model_dump(by_alias=True)["redactions"] == {
        "phone": 1,
        "idCard": 2,
        "bankCard": 3,
        "email": 4,
        "ipv4": 5,
    }


@pytest.mark.parametrize("field", ["phone", "idCard", "bankCard", "email", "ipv4"])
def test_summary_rejects_negative_redaction_counts(field: str) -> None:
    redactions = {
        "phone": 1,
        "idCard": 2,
        "bankCard": 3,
        "email": 4,
        "ipv4": 5,
    }
    redactions[field] = -1

    with pytest.raises(ValidationError):
        MarkdownCleaningSummaryPublic.model_validate(
            {
                "duplicateParagraphsRemoved": 2,
                "redactions": redactions,
                "formattingChanges": 3,
            }
        )


def test_domain_error_keeps_a_stable_code_and_safe_message() -> None:
    error = MarkdownCleaningDomainError(
        MarkdownCleaningApiErrorCode.TASK_NOT_FOUND,
        "任务不存在",
        http_status=404,
    )

    assert error.code is MarkdownCleaningApiErrorCode.TASK_NOT_FOUND
    assert error.safe_message == "任务不存在"
    assert error.http_status == 404
