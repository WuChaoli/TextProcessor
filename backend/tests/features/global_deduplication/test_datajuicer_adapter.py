import json
import uuid
from pathlib import Path

import httpx
import pytest

from app.features.global_deduplication.adapters.datajuicer import (
    DataJuicerAdapter,
    DataJuicerSubmitRequest,
)
from app.features.global_deduplication.errors import (
    GlobalDeduplicationErrorCode,
    GlobalDeduplicationProcessingError,
)


def build_adapter(
    handler: httpx.MockTransport,
) -> DataJuicerAdapter:
    return DataJuicerAdapter(
        base_url="http://datajuicer.internal",
        client=httpx.Client(transport=handler),
    )


def assert_error(
    error: pytest.ExceptionInfo[GlobalDeduplicationProcessingError],
    code: GlobalDeduplicationErrorCode,
) -> None:
    assert error.value.code is code


def test_submit_uses_task_id_and_paths() -> None:
    task_id = uuid.uuid7()
    job_id = uuid.uuid7()
    observed: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(json.loads(request.read()))
        return httpx.Response(
            202,
            json={
                "jobId": str(job_id),
                "requestId": str(task_id),
                "profile": "text_exact_minhash_v1",
                "status": "queued",
            },
        )

    request = DataJuicerSubmitRequest(
        request_id=task_id,
        input_path=Path("/shared/input.jsonl"),
        output_path=Path("/shared/result.jsonl"),
        profile="text_exact_minhash_v1",
    )
    submission = build_adapter(httpx.MockTransport(handler)).submit(request)

    assert submission.job_id == job_id
    assert submission.request_id == task_id
    assert submission.status == "queued"
    assert len(observed) == 1
    assert observed[0]["requestId"] == str(task_id)
    assert observed[0]["inputPath"] == str(request.input_path)


def test_submit_rejects_mismatched_idempotency_response() -> None:
    task_id = uuid.uuid7()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            202,
            json={
                "jobId": str(uuid.uuid7()),
                "requestId": str(uuid.uuid7()),
                "profile": "text_exact_minhash_v1",
                "status": "queued",
            },
        )

    with pytest.raises(GlobalDeduplicationProcessingError) as error:
        build_adapter(httpx.MockTransport(handler)).submit(
            DataJuicerSubmitRequest(
                request_id=task_id,
                input_path=Path("/shared/input.jsonl"),
                output_path=Path("/shared/result.jsonl"),
                profile="text_exact_minhash_v1",
            )
        )

    assert_error(error, GlobalDeduplicationErrorCode.INVALID_PROCESSOR_RESPONSE)


@pytest.mark.parametrize(
    ("status_code", "detail_code", "expected"),
    [
        (
            409,
            "IDEMPOTENCY_CONFLICT",
            GlobalDeduplicationErrorCode.PROCESSOR_IDEMPOTENCY_CONFLICT,
        ),
        (
            422,
            "INVALID_REQUEST",
            GlobalDeduplicationErrorCode.PROCESSOR_REQUEST_REJECTED,
        ),
        (
            503,
            "SERVICE_NOT_READY",
            GlobalDeduplicationErrorCode.PROCESSOR_UNAVAILABLE,
        ),
    ],
)
def test_submit_maps_http_errors(
    status_code: int,
    detail_code: str,
    expected: GlobalDeduplicationErrorCode,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            json={"detail": {"code": detail_code, "message": "internal detail"}},
        )

    with pytest.raises(GlobalDeduplicationProcessingError) as error:
        build_adapter(httpx.MockTransport(handler)).submit(
            DataJuicerSubmitRequest(
                request_id=uuid.uuid7(),
                input_path=Path("/shared/input.jsonl"),
                output_path=Path("/shared/result.jsonl"),
                profile="text_exact_minhash_v1",
            )
        )

    assert_error(error, expected)
    assert "internal detail" not in error.value.safe_message


def test_submit_timeout_is_uncertain() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("after request", request=request)

    with pytest.raises(GlobalDeduplicationProcessingError) as error:
        build_adapter(httpx.MockTransport(handler)).submit(
            DataJuicerSubmitRequest(
                request_id=uuid.uuid7(),
                input_path=Path("/shared/input.jsonl"),
                output_path=Path("/shared/result.jsonl"),
                profile="text_exact_minhash_v1",
            )
        )

    assert_error(
        error,
        GlobalDeduplicationErrorCode.PROCESSOR_SUBMISSION_UNCERTAIN,
    )


def test_get_job_validates_terminal_contract() -> None:
    task_id = uuid.uuid7()
    job_id = uuid.uuid7()
    output = Path("/shared/result.jsonl")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "jobId": str(job_id),
                "requestId": str(task_id),
                "profile": "text_exact_minhash_v1",
                "status": "succeeded",
                "progress": {
                    "phase": "completed",
                    "total": 6,
                    "processed": 6,
                    "percent": 100,
                },
                "result": {
                    "outputPath": str(output),
                    "outputSha256": "a" * 64,
                },
                "error": None,
            },
        )

    job = build_adapter(httpx.MockTransport(handler)).get_job(
        job_id,
        expected_request_id=task_id,
        expected_profile="text_exact_minhash_v1",
        expected_output_path=output,
    )

    assert job.status == "succeeded"
    assert job.result is not None
    assert job.result.output_sha256 == "a" * 64


def test_get_job_rejects_wrong_output_path() -> None:
    task_id = uuid.uuid7()
    job_id = uuid.uuid7()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "jobId": str(job_id),
                "requestId": str(task_id),
                "profile": "text_exact_minhash_v1",
                "status": "succeeded",
                "progress": {
                    "phase": "completed",
                    "total": 1,
                    "processed": 1,
                    "percent": 100,
                },
                "result": {
                    "outputPath": "/other/result.jsonl",
                    "outputSha256": "b" * 64,
                },
                "error": None,
            },
        )

    with pytest.raises(GlobalDeduplicationProcessingError) as error:
        build_adapter(httpx.MockTransport(handler)).get_job(
            job_id,
            expected_request_id=task_id,
            expected_profile="text_exact_minhash_v1",
            expected_output_path=Path("/shared/result.jsonl"),
        )

    assert_error(error, GlobalDeduplicationErrorCode.INVALID_PROCESSOR_RESPONSE)


def test_get_job_accepts_cancelled_without_external_error_body() -> None:
    task_id = uuid.uuid7()
    job_id = uuid.uuid7()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "jobId": str(job_id),
                "requestId": str(task_id),
                "profile": "text_exact_minhash_v1",
                "status": "cancelled",
                "progress": {
                    "phase": "cancelled",
                    "total": None,
                    "processed": 0,
                    "percent": 0,
                },
                "result": None,
                "error": None,
            },
        )

    job = build_adapter(httpx.MockTransport(handler)).get_job(
        job_id,
        expected_request_id=task_id,
        expected_profile="text_exact_minhash_v1",
        expected_output_path=Path("/shared/result.jsonl"),
    )

    assert job.status == "cancelled"
    assert job.error is None
