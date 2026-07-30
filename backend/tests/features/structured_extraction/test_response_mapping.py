import uuid

import pytest

from app.features.structured_extraction.models import (
    ExtractionTask,
    ExtractionTaskStatus,
)
from app.features.structured_extraction.routes import task_to_public


@pytest.mark.parametrize(
    ("task_status", "has_result", "has_error"),
    [
        (ExtractionTaskStatus.PENDING, False, False),
        (ExtractionTaskStatus.QUEUED, False, False),
        (ExtractionTaskStatus.RUNNING, False, False),
        (ExtractionTaskStatus.SUCCEEDED, True, False),
        (ExtractionTaskStatus.FAILED, False, True),
        (ExtractionTaskStatus.CANCELLED, False, True),
    ],
)
def test_response_matrix_never_contains_markdown_body(
    task_status: ExtractionTaskStatus,
    has_result: bool,
    has_error: bool,
) -> None:
    task = ExtractionTask(
        caller_id=uuid.uuid4(),
        session_id="session-1",
        file_id="file-1",
        request_fingerprint="a" * 64,
        file_storage_path="/allowed/input/sample.txt",
        file_oss_url=None,
        selected_input_type="local",
        target_path="/allowed/output/sample.md",
        status=task_status,
        result_metadata=(
            {
                "content": "# secret markdown",
                "markdown": "# secret markdown",
            }
            if task_status is ExtractionTaskStatus.SUCCEEDED
            else None
        ),
        error_code=(
            "PROCESSING_FAILED"
            if task_status
            in {ExtractionTaskStatus.FAILED, ExtractionTaskStatus.CANCELLED}
            else None
        ),
        error_message=(
            "处理失败"
            if task_status
            in {ExtractionTaskStatus.FAILED, ExtractionTaskStatus.CANCELLED}
            else None
        ),
    )

    response = task_to_public(task).model_dump(by_alias=True, mode="json")

    assert (response["result"] is not None) is has_result
    assert (response["error"] is not None) is has_error
    assert "content" not in response
    assert "markdown" not in response
    if response["result"]:
        assert set(response["result"]) == {
            "fileStoragePath",
            "fileOssUrl",
            "targetPath",
        }
