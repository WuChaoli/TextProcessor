from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from starlette.testclient import TestClient

from datajuicer_service.api.schemas import JobProgressPublic
from datajuicer_service.jobs.dispatcher import ExecutionMessage
from datajuicer_service.jobs.models import DataJuicerJob
from datajuicer_service.jobs.repository import (
    CreateJobResult,
    IdempotencyConflict,
    JobCreate,
    JobError,
)
from datajuicer_service.jobs.service import JobService
from datajuicer_service.jobs.state_machine import JobStatus
from datajuicer_service.main import create_app

NOW = datetime(2026, 7, 31, 10, 0, tzinfo=UTC)
VALID_REQUEST = {
    "requestId": "0198f000-0000-7000-8000-000000000001",
    "profile": "text_exact_minhash_v1",
    "inputPath": "C:/staging/input.jsonl",
    "outputPath": "C:/staging/output.jsonl",
}


class FakeRepository:
    def __init__(self) -> None:
        self.jobs: dict[UUID, DataJuicerJob] = {}
        self.by_request_id: dict[str, DataJuicerJob] = {}

    def create_or_get(
        self,
        request: JobCreate,
        *,
        now: datetime,
    ) -> CreateJobResult:
        existing = self.by_request_id.get(request.request_id)
        if existing is not None:
            if existing.request_fingerprint != request.fingerprint:
                raise IdempotencyConflict("IDEMPOTENCY_CONFLICT")
            return CreateJobResult(existing, created=False)
        job = make_job(
            request_id=request.request_id,
            request_fingerprint=request.fingerprint,
            input_path=request.input_path,
            output_path=request.output_path,
            status=JobStatus.PENDING,
        )
        self.jobs[job.job_id] = job
        self.by_request_id[job.request_id] = job
        return CreateJobResult(job, created=True)

    def get(self, job_id: UUID) -> DataJuicerJob | None:
        return self.jobs.get(job_id)

    def mark_queued(self, job_id: UUID, *, now: datetime) -> None:
        job = self.jobs[job_id]
        job.status = JobStatus.QUEUED
        job.processing_phase = "queued"
        job.queued_at = now
        job.updated_at = now

    def mark_failed(
        self,
        job_id: UUID,
        lease_token: UUID | None,
        error: JobError,
        *,
        now: datetime,
    ) -> None:
        job = self.jobs[job_id]
        job.status = JobStatus.FAILED
        job.processing_phase = "failed"
        job.error_code = error.code
        job.error_message = error.message
        job.finished_at = now
        job.updated_at = now


class FakeDispatcher:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.messages: list[ExecutionMessage] = []

    def enqueue(self, message: ExecutionMessage) -> None:
        if self.fail:
            raise RuntimeError("broker unavailable")
        self.messages.append(message)


def make_job(
    *,
    request_id: str,
    request_fingerprint: str = "f" * 64,
    input_path: str = "C:/staging/input.jsonl",
    output_path: str = "C:/staging/output.jsonl",
    status: JobStatus,
) -> DataJuicerJob:
    return DataJuicerJob(
        job_id=uuid4(),
        request_id=request_id,
        request_fingerprint=request_fingerprint,
        profile="text_exact_minhash_v1",
        input_path=input_path,
        output_path=output_path,
        status=status,
        processing_phase=status.value,
        progress_total=None,
        progress_processed=0,
        progress_percent=0,
        attempt_count=0,
        max_attempts=3,
        lease_token=None,
        lease_expires_at=None,
        processing_deadline=NOW + timedelta(hours=1),
        input_sha256=None,
        input_count=None,
        prepared_output_sha256=None,
        staging_output_path=None,
        output_sha256=None,
        published_at=None,
        error_code=None,
        error_message=None,
        created_at=NOW,
        queued_at=None,
        started_at=None,
        finished_at=None,
        updated_at=NOW,
    )


def make_client(
    repository: FakeRepository | None = None,
    dispatcher: FakeDispatcher | None = None,
    *,
    ready: bool = True,
) -> tuple[TestClient, FakeRepository, FakeDispatcher]:
    repository = repository or FakeRepository()
    dispatcher = dispatcher or FakeDispatcher()

    @contextmanager
    def repository_factory() -> Iterator[FakeRepository]:
        yield repository

    service = JobService(
        repository_factory=repository_factory,
        dispatcher=dispatcher,
        max_attempts=3,
        job_timeout_seconds=3600,
        now=lambda: NOW,
    )
    app = create_app(service, readiness_check=lambda: ready)
    return TestClient(app), repository, dispatcher


def test_create_job_returns_202_and_dispatches_minimal_message() -> None:
    client, _repository, dispatcher = make_client()

    response = client.post("/v1/jobs", json=VALID_REQUEST)

    assert response.status_code == 202
    body = response.json()
    assert body["requestId"] == VALID_REQUEST["requestId"]
    assert body["profile"] == "text_exact_minhash_v1"
    assert body["status"] == "queued"
    assert dispatcher.messages == [
        ExecutionMessage(
            job_id=UUID(body["jobId"]),
            task_type="datajuicer_job",
            schema_version=1,
        )
    ]


def test_identical_request_returns_original_job_without_redispatch() -> None:
    client, _repository, dispatcher = make_client()

    first = client.post("/v1/jobs", json=VALID_REQUEST)
    second = client.post("/v1/jobs", json=VALID_REQUEST)

    assert second.status_code == 202
    assert second.json() == first.json()
    assert len(dispatcher.messages) == 1


def test_same_request_id_with_different_path_returns_409() -> None:
    client, _repository, _dispatcher = make_client()
    assert client.post("/v1/jobs", json=VALID_REQUEST).status_code == 202
    changed = {**VALID_REQUEST, "outputPath": "C:/staging/changed.jsonl"}

    response = client.post("/v1/jobs", json=changed)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_enqueue_failure_marks_job_failed_and_returns_503() -> None:
    client, repository, _dispatcher = make_client(dispatcher=FakeDispatcher(fail=True))

    response = client.post("/v1/jobs", json=VALID_REQUEST)

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "QUEUE_SUBMISSION_FAILED"
    job = next(iter(repository.jobs.values()))
    assert job.status is JobStatus.FAILED
    assert job.error_code == "QUEUE_SUBMISSION_FAILED"


def test_get_job_maps_progress_and_null_result_error() -> None:
    client, repository, _dispatcher = make_client()
    created = client.post("/v1/jobs", json=VALID_REQUEST).json()

    response = client.get(f"/v1/jobs/{created['jobId']}")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "queued"
    assert body["progress"] == {
        "phase": "queued",
        "total": None,
        "processed": 0,
        "percent": 0,
    }
    assert body["result"] is None
    assert body["error"] is None
    assert repository.get(UUID(created["jobId"])) is not None


def test_get_unknown_job_returns_stable_404() -> None:
    client, _repository, _dispatcher = make_client()

    response = client.get(f"/v1/jobs/{uuid4()}")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "JOB_NOT_FOUND"


@pytest.mark.parametrize(
    "payload",
    [
        {**VALID_REQUEST, "profile": "arbitrary_recipe"},
        {**VALID_REQUEST, "inputPath": "relative/input.jsonl"},
        {**VALID_REQUEST, "unexpected": True},
    ],
)
def test_invalid_request_returns_stable_error(payload: dict[str, object]) -> None:
    client, _repository, _dispatcher = make_client()

    response = client.post("/v1/jobs", json=payload)

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INVALID_REQUEST"


def test_health_and_readiness_are_separate() -> None:
    client, _repository, _dispatcher = make_client(ready=False)

    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code == 503


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("total", -1),
        ("processed", -1),
        ("percent", -1),
        ("percent", 101),
    ],
)
def test_progress_schema_rejects_invalid_counters(field: str, value: int) -> None:
    payload = {
        "phase": "running",
        "total": 10,
        "processed": 5,
        "percent": 50,
        field: value,
    }

    with pytest.raises(ValidationError):
        JobProgressPublic.model_validate(payload)
