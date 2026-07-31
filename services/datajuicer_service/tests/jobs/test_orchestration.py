import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy.orm import Session, sessionmaker

from datajuicer_service.jobs.orchestration import (
    JobOrchestrator,
    RetryableJobError,
)
from datajuicer_service.jobs.repository import (
    JobCreate,
    JobPrepared,
    JobRepository,
)
from datajuicer_service.jobs.state_machine import JobStatus
from datajuicer_service.profiles.io import InputLimits
from datajuicer_service.profiles.text_exact_minhash_v1 import TextExactMinhashV1

NOW = datetime(2026, 7, 31, 10, 0, tzinfo=UTC)
LIMITS = InputLimits(max_records=100, max_bytes=1024 * 1024, max_text_chars=100_000)


class Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class ProfileSpy:
    def __init__(self, delegate: TextExactMinhashV1) -> None:
        self.delegate = delegate
        self.calls = 0

    def execute(self, *args, **kwargs):
        self.calls += 1
        return self.delegate.execute(*args, **kwargs)


class FailingProfile:
    def execute(self, *args, **kwargs):
        raise OSError("temporary storage failure")


def repository_factory(
    sessions: sessionmaker[Session],
    *,
    lease_seconds: int = 60,
):
    @contextmanager
    def factory() -> Iterator[JobRepository]:
        with sessions() as session:
            yield JobRepository(session, lease_seconds=lease_seconds)

    return factory


def create_queued_job(
    sessions: sessionmaker[Session],
    input_path: Path,
    output_path: Path,
    *,
    now: datetime = NOW,
) -> UUID:
    with sessions() as session:
        repository = JobRepository(session)
        job = repository.create_or_get(
            JobCreate(
                request_id="request-1",
                profile="text_exact_minhash_v1",
                input_path=str(input_path),
                output_path=str(output_path),
                max_attempts=3,
                processing_deadline=now + timedelta(hours=1),
            ),
            now=now,
        ).job
        repository.mark_queued(job.job_id, now=now)
        return job.job_id


def test_duplicate_execute_message_runs_profile_once(
    orchestration_session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "input.jsonl"
    output_path = tmp_path / "output.jsonl"
    input_path.write_text(
        '{"uid":0,"text":"same"}\n{"uid":1,"text":" same "}\n',
        encoding="utf-8",
    )
    job_id = create_queued_job(orchestration_session_factory, input_path, output_path)
    profile = ProfileSpy(TextExactMinhashV1(LIMITS))
    orchestrator = JobOrchestrator(
        repository_factory=repository_factory(orchestration_session_factory),
        profile_resolver=lambda _name: profile,
        now=Clock(NOW),
    )

    orchestrator.execute(job_id)
    orchestrator.execute(job_id)

    assert profile.calls == 1
    with orchestration_session_factory() as session:
        job = JobRepository(session).get(job_id)
        assert job is not None
        assert job.status is JobStatus.SUCCEEDED
        assert job.output_sha256 == hashlib.sha256(output_path.read_bytes()).hexdigest()


def test_published_digest_recovers_success_without_profile(
    orchestration_session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "input.jsonl"
    output_path = tmp_path / "output.jsonl"
    input_path.write_text('{"uid":0,"text":"only"}\n', encoding="utf-8")
    output_path.write_text(
        '{"uid":0,"clusterId":null,"representative":true,"method":null}\n',
        encoding="utf-8",
    )
    job_id = create_queued_job(orchestration_session_factory, input_path, output_path)
    output_sha256 = hashlib.sha256(output_path.read_bytes()).hexdigest()
    with orchestration_session_factory() as session:
        repository = JobRepository(session, lease_seconds=1)
        lease = repository.acquire_execution(job_id, now=NOW)
        assert lease is not None
        repository.mark_prepared(
            job_id,
            lease.token,
            JobPrepared(
                output_sha256=output_sha256,
                staging_output_path=str(tmp_path / ".output.part"),
                input_sha256=hashlib.sha256(input_path.read_bytes()).hexdigest(),
                input_count=1,
            ),
            now=NOW,
        )

    profile = ProfileSpy(TextExactMinhashV1(LIMITS))
    orchestrator = JobOrchestrator(
        repository_factory=repository_factory(
            orchestration_session_factory,
            lease_seconds=1,
        ),
        profile_resolver=lambda _name: profile,
        now=Clock(NOW + timedelta(seconds=2)),
    )

    orchestrator.execute(job_id)

    assert profile.calls == 0
    with orchestration_session_factory() as session:
        job = JobRepository(session).get(job_id)
        assert job is not None
        assert job.status is JobStatus.SUCCEEDED
        assert job.output_sha256 == output_sha256


def test_deterministic_input_error_marks_job_failed(
    orchestration_session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "missing.jsonl"
    output_path = tmp_path / "output.jsonl"
    job_id = create_queued_job(orchestration_session_factory, input_path, output_path)
    orchestrator = JobOrchestrator(
        repository_factory=repository_factory(orchestration_session_factory),
        profile_resolver=lambda _name: TextExactMinhashV1(LIMITS),
        now=Clock(NOW),
    )

    orchestrator.execute(job_id)

    with orchestration_session_factory() as session:
        job = JobRepository(session).get(job_id)
        assert job is not None
        assert job.status is JobStatus.FAILED
        assert job.error_code == "INPUT_NOT_FOUND"


def test_transient_profile_error_releases_lease_for_retry(
    orchestration_session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "input.jsonl"
    input_path.write_text('{"uid":0,"text":"only"}\n', encoding="utf-8")
    job_id = create_queued_job(
        orchestration_session_factory,
        input_path,
        tmp_path / "output.jsonl",
    )
    orchestrator = JobOrchestrator(
        repository_factory=repository_factory(orchestration_session_factory),
        profile_resolver=lambda _name: FailingProfile(),
        now=Clock(NOW),
    )

    with pytest.raises(RetryableJobError):
        orchestrator.execute(job_id)

    with orchestration_session_factory() as session:
        job = JobRepository(session).get(job_id)
        assert job is not None
        assert job.status is JobStatus.RUNNING
        assert job.lease_expires_at is not None
        assert job.lease_expires_at.replace(tzinfo=UTC) == NOW
        assert job.error_code is None


def test_expired_queued_job_is_finalized_as_timeout(
    orchestration_session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "input.jsonl"
    output_path = tmp_path / "output.jsonl"
    input_path.write_text('{"uid":0,"text":"only"}\n', encoding="utf-8")
    job_id = create_queued_job(
        orchestration_session_factory,
        input_path,
        output_path,
        now=NOW - timedelta(hours=2),
    )
    orchestrator = JobOrchestrator(
        repository_factory=repository_factory(orchestration_session_factory),
        profile_resolver=lambda _name: TextExactMinhashV1(LIMITS),
        now=Clock(NOW),
    )

    orchestrator.execute(job_id)

    with orchestration_session_factory() as session:
        job = JobRepository(session).get(job_id)
        assert job is not None
        assert job.status is JobStatus.FAILED
        assert job.error_code == "JOB_TIMEOUT"
    assert not output_path.exists()
