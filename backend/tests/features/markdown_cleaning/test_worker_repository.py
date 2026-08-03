import uuid
from collections.abc import Generator
from datetime import datetime, timedelta

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.features.markdown_cleaning.repository import MarkdownCleaningTaskRepository
from app.features.markdown_cleaning.state_machine import MarkdownCleaningTaskStatus
from app.features.markdown_cleaning.task_models import MarkdownCleaningTask
from app.features.markdown_cleaning.worker_models import (
    MarkdownCleaningProcessingPhase,
)
from app.models import User  # noqa: F401

NOW = datetime(2026, 8, 3, 8, 0)


@pytest.fixture
def session() -> Generator[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def make_task(
    *,
    status: MarkdownCleaningTaskStatus = MarkdownCleaningTaskStatus.QUEUED,
    task_id: uuid.UUID | None = None,
    file_id: str | None = None,
    attempt_count: int = 0,
    max_attempts: int = 3,
    queued_at: datetime | None = NOW,
    lease_expires_at: datetime | None = None,
    lease_token: str | None = None,
) -> MarkdownCleaningTask:
    return MarkdownCleaningTask(
        id=task_id or uuid.uuid7(),
        caller_id=uuid.uuid7(),
        session_id="session-1",
        file_id=file_id or f"file-{uuid.uuid4()}",
        request_fingerprint="a" * 64,
        file_storage_path="/data/input.md",
        selected_input_type="local",
        target_path="/data/output.md",
        status=status,
        attempt_count=attempt_count,
        max_attempts=max_attempts,
        queued_at=queued_at,
        lease_expires_at=lease_expires_at,
        lease_token=lease_token,
    )


def test_acquire_queued_updates_status_and_lease(session: Session) -> None:
    task = make_task()
    session.add(task)
    session.commit()

    repository = MarkdownCleaningTaskRepository(session)
    acquired = repository.acquire_queued(task.id, now=NOW, lease_seconds=30)
    assert acquired is not None
    assert acquired.status is MarkdownCleaningTaskStatus.RUNNING
    assert acquired.attempt_count == 1
    assert acquired.lease_token is not None
    assert acquired.processing_phase == MarkdownCleaningProcessingPhase.CLAIMING_TASK
    assert acquired.lease_expires_at == NOW + timedelta(seconds=30)


def test_acquire_queued_rejects_when_attempt_limit_reached(session: Session) -> None:
    task = make_task(
        status=MarkdownCleaningTaskStatus.QUEUED,
        attempt_count=3,
        max_attempts=3,
    )
    session.add(task)
    session.commit()

    assert (
        MarkdownCleaningTaskRepository(session).acquire_queued(
            task.id, now=NOW, lease_seconds=30
        )
        is None
    )


def test_acquire_queued_is_single_consumer(session: Session) -> None:
    task = make_task()
    session.add(task)
    session.commit()
    repository = MarkdownCleaningTaskRepository(session)

    first = repository.acquire_queued(task.id, now=NOW, lease_seconds=30)
    second = repository.acquire_queued(task.id, now=NOW, lease_seconds=30)

    assert first is not None
    assert second is None


def test_renew_lease_rejects_stale_token(session: Session) -> None:
    task = make_task()
    session.add(task)
    session.commit()
    repository = MarkdownCleaningTaskRepository(session)
    acquired = repository.acquire_queued(task.id, now=NOW, lease_seconds=10)
    assert acquired is not None
    assert acquired.lease_token is not None

    assert repository.renew_lease(
        task.id,
        lease_token="invalid",
        now=NOW + timedelta(seconds=1),
        lease_seconds=10,
    ) is False

    assert (
        repository.renew_lease(
            task.id,
            lease_token=acquired.lease_token,
            now=NOW + timedelta(seconds=1),
            lease_seconds=10,
        )
        is True
    )


def test_save_prepared_and_mark_succeeded(session: Session) -> None:
    task = make_task()
    session.add(task)
    session.commit()
    repository = MarkdownCleaningTaskRepository(session)
    acquired = repository.acquire_queued(task.id, now=NOW, lease_seconds=20)
    assert acquired is not None
    assert acquired.lease_token is not None

    assert repository.save_prepared(
        task.id,
        lease_token=acquired.lease_token,
        staging_path="/staging/task.md",
        input_sha256="1" * 64,
        now=NOW + timedelta(seconds=1),
    )
    prepared = repository.get(task.id)
    assert prepared is not None
    assert prepared.processing_phase == MarkdownCleaningProcessingPhase.SAVING_PREPARED

    assert repository.mark_succeeded(
        task.id,
        lease_token=acquired.lease_token,
        now=NOW + timedelta(seconds=2),
        output_sha256="2" * 64,
        prepared_output_sha256="3" * 64,
    )
    succeeded = repository.get(task.id)
    assert succeeded is not None
    assert succeeded.status is MarkdownCleaningTaskStatus.SUCCEEDED
    assert succeeded.progress_percent == 100
    assert succeeded.lease_token is None
    assert succeeded.processing_phase == MarkdownCleaningProcessingPhase.SUCCEEDED


def test_mark_failed_clears_lease_and_records_error(session: Session) -> None:
    task = make_task()
    session.add(task)
    session.commit()
    repository = MarkdownCleaningTaskRepository(session)
    acquired = repository.acquire_queued(task.id, now=NOW, lease_seconds=20)
    assert acquired is not None
    assert acquired.lease_token is not None

    assert repository.mark_failed(
        task.id,
        lease_token=acquired.lease_token,
        now=NOW + timedelta(seconds=1),
        error_code="FAILED",
        error_message="worker failed",
    )

    failed = repository.get(task.id)
    assert failed is not None
    assert failed.status is MarkdownCleaningTaskStatus.FAILED
    assert failed.lease_token is None
    assert failed.processing_phase == MarkdownCleaningProcessingPhase.FAILED
    assert failed.error_code == "FAILED"
    assert failed.error_message == "worker failed"


def test_recoverable_lists_and_active_count(session: Session) -> None:
    recoverable_queued = make_task(
        file_id="queued-stale",
        status=MarkdownCleaningTaskStatus.QUEUED,
        lease_expires_at=NOW - timedelta(seconds=1),
    )
    blocked_queued = make_task(
        file_id="queued-blocked",
        status=MarkdownCleaningTaskStatus.QUEUED,
        lease_expires_at=NOW + timedelta(minutes=10),
        lease_token="t1",
    )
    running_active = make_task(
        file_id="running-active",
        status=MarkdownCleaningTaskStatus.RUNNING,
        lease_token="t2",
        lease_expires_at=NOW + timedelta(minutes=5),
    )
    running_expired = make_task(
        file_id="running-expired",
        status=MarkdownCleaningTaskStatus.RUNNING,
        lease_token="t3",
        lease_expires_at=NOW - timedelta(minutes=1),
    )
    session.add_all(
        [recoverable_queued, blocked_queued, running_active, running_expired]
    )
    session.commit()

    repository = MarkdownCleaningTaskRepository(session)
    recovered = repository.list_recoverable_queued(now=NOW, limit=10)
    assert len(recovered) == 1
    assert recovered[0].file_id == "queued-stale"

    expired_running = repository.list_recoverable_running(now=NOW, limit=10)
    assert [task.file_id for task in expired_running] == ["running-expired"]

    assert repository.count_active_running(now=NOW) == 1
