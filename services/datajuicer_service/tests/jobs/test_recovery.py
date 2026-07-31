from datetime import timedelta
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from datajuicer_service.jobs.dispatcher import ExecutionMessage
from datajuicer_service.jobs.orchestration import JobOrchestrator
from datajuicer_service.jobs.repository import JobCreate, JobRepository
from datajuicer_service.jobs.state_machine import JobStatus

from .test_orchestration import NOW, Clock, repository_factory


class RecordingDispatcher:
    def __init__(self) -> None:
        self.messages: list[ExecutionMessage] = []

    def enqueue(self, message: ExecutionMessage) -> None:
        self.messages.append(message)


def test_recovery_only_redispatches_stale_jobs(
    orchestration_session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    old_time = NOW - timedelta(minutes=10)
    with orchestration_session_factory() as session:
        repository = JobRepository(session, recovery_age_seconds=30)
        pending = repository.create_or_get(
            JobCreate(
                request_id="pending",
                profile="text_exact_minhash_v1",
                input_path=str(tmp_path / "pending-input.jsonl"),
                output_path=str(tmp_path / "pending-output.jsonl"),
                max_attempts=3,
                processing_deadline=NOW + timedelta(hours=1),
            ),
            now=old_time,
        ).job
        queued = repository.create_or_get(
            JobCreate(
                request_id="queued",
                profile="text_exact_minhash_v1",
                input_path=str(tmp_path / "queued-input.jsonl"),
                output_path=str(tmp_path / "queued-output.jsonl"),
                max_attempts=3,
                processing_deadline=NOW + timedelta(hours=1),
            ),
            now=old_time,
        ).job
        repository.mark_queued(queued.job_id, now=old_time)
        running = repository.create_or_get(
            JobCreate(
                request_id="running",
                profile="text_exact_minhash_v1",
                input_path=str(tmp_path / "running-input.jsonl"),
                output_path=str(tmp_path / "running-output.jsonl"),
                max_attempts=3,
                processing_deadline=NOW + timedelta(hours=1),
            ),
            now=old_time,
        ).job
        repository.mark_queued(running.job_id, now=old_time)
        running_lease = JobRepository(session, lease_seconds=1).acquire_execution(
            running.job_id,
            now=old_time,
        )
        assert running_lease is not None

    dispatcher = RecordingDispatcher()
    profile_calls = 0

    def forbidden_profile(_name: str):
        nonlocal profile_calls
        profile_calls += 1
        raise AssertionError("recovery must not execute profiles")

    orchestrator = JobOrchestrator(
        repository_factory=repository_factory(orchestration_session_factory),
        profile_resolver=forbidden_profile,
        dispatcher=dispatcher,
        now=Clock(NOW),
        recovery_batch_size=10,
    )

    recovered = orchestrator.recover()
    repeated = orchestrator.recover()

    assert recovered == 3
    assert repeated == 0
    assert {message.job_id for message in dispatcher.messages} == {
        pending.job_id,
        queued.job_id,
        running.job_id,
    }
    assert profile_calls == 0
    with orchestration_session_factory() as session:
        pending_after = JobRepository(session).get(pending.job_id)
        assert pending_after is not None
        assert pending_after.status is JobStatus.QUEUED
