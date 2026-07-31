from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from datajuicer_service.jobs.models import DataJuicerJob
from datajuicer_service.jobs.repository import (
    IdempotencyConflict,
    JobCreate,
    JobError,
    JobProgress,
    JobRepository,
    JobResult,
)
from datajuicer_service.jobs.state_machine import JobStatus

NOW = datetime(2026, 7, 31, 10, 0, tzinfo=UTC)


def make_request(
    request_id: str = "0198f000-0000-7000-8000-000000000001",
    output_path: str = "C:/staging/output.jsonl",
) -> JobCreate:
    return JobCreate(
        request_id=request_id,
        profile="text_exact_minhash_v1",
        input_path="C:/staging/input.jsonl",
        output_path=output_path,
        max_attempts=3,
        processing_deadline=NOW + timedelta(hours=1),
    )


def test_concurrent_request_id_has_one_job(
    session_factory: sessionmaker[Session],
) -> None:
    request = make_request()

    def create() -> tuple[UUID, bool]:
        with session_factory() as session:
            result = JobRepository(session).create_or_get(request, now=NOW)
            return result.job.job_id, result.created

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: create(), range(2)))

    with session_factory() as session:
        row_count = session.scalar(select(func.count()).select_from(DataJuicerJob))
    assert row_count == 1
    assert results[0][0] == results[1][0]
    assert sorted(created for _job_id, created in results) == [False, True]


def test_same_request_id_with_different_fingerprint_conflicts(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        repository = JobRepository(session)
        repository.create_or_get(make_request(), now=NOW)

    with session_factory() as session:
        with pytest.raises(IdempotencyConflict):
            JobRepository(session).create_or_get(
                make_request(output_path="C:/staging/changed.jsonl"),
                now=NOW,
            )


def test_execution_lease_allows_only_one_worker(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        repository = JobRepository(session, lease_seconds=60)
        created = repository.create_or_get(make_request(), now=NOW)
        repository.mark_queued(created.job.job_id, now=NOW)
        job_id = created.job.job_id

    with session_factory() as first_session:
        first = JobRepository(first_session, lease_seconds=60).acquire_execution(
            job_id,
            now=NOW,
        )
    with session_factory() as second_session:
        second = JobRepository(second_session, lease_seconds=60).acquire_execution(
            job_id,
            now=NOW,
        )

    assert first is not None
    assert second is None
    with session_factory() as session:
        job = JobRepository(session).get(job_id)
        assert job is not None
        assert job.status is JobStatus.RUNNING
        assert job.attempt_count == 1


def test_progress_and_success_require_current_lease(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        repository = JobRepository(session, lease_seconds=60)
        created = repository.create_or_get(make_request(), now=NOW)
        repository.mark_queued(created.job.job_id, now=NOW)
        lease = repository.acquire_execution(created.job.job_id, now=NOW)
        assert lease is not None
        repository.update_progress(
            created.job.job_id,
            lease.token,
            JobProgress(phase="minhash_computing", total=10, processed=4, percent=40),
            now=NOW,
        )
        repository.mark_succeeded(
            created.job.job_id,
            lease.token,
            JobResult(
                output_sha256="a" * 64,
                published_at=NOW,
                input_sha256="b" * 64,
                input_count=10,
            ),
            now=NOW,
        )
        job = repository.get(created.job.job_id)

    assert job is not None
    assert job.status is JobStatus.SUCCEEDED
    assert job.progress_percent == 100
    assert job.output_sha256 == "a" * 64
    assert job.lease_token is None


def test_failure_and_recovery_query_persist_stable_error(
    session_factory: sessionmaker[Session],
) -> None:
    old_time = NOW - timedelta(minutes=10)
    with session_factory() as session:
        repository = JobRepository(session, recovery_age_seconds=30)
        pending = repository.create_or_get(
            make_request(request_id="pending"),
            now=old_time,
        ).job
        failed = repository.create_or_get(
            make_request(request_id="failed"),
            now=NOW,
        ).job
        repository.mark_failed(
            failed.job_id,
            lease_token=None,
            error=JobError(code="QUEUE_SUBMISSION_FAILED", message="任务入队失败"),
            now=NOW,
        )

        recoverable = repository.find_recoverable(now=NOW, limit=10)
        failed_job = repository.get(failed.job_id)

    assert [job.job_id for job in recoverable] == [pending.job_id]
    assert failed_job is not None
    assert failed_job.status is JobStatus.FAILED
    assert failed_job.error_code == "QUEUE_SUBMISSION_FAILED"
    assert failed_job.error_message == "任务入队失败"
