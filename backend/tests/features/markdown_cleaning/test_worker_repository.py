import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.features.markdown_cleaning.repository import MarkdownCleaningTaskRepository
from app.features.markdown_cleaning.state_machine import MarkdownCleaningTaskStatus
from app.features.markdown_cleaning.task_models import MarkdownCleaningTask
from app.features.markdown_cleaning.worker_models import (
    MarkdownCleaningProcessingPhase,
)
from app.models import User

NOW = datetime(2026, 8, 3, 8, 0, tzinfo=UTC)


@pytest.fixture
def session() -> Generator[Session]:
    _ = User
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
    last_dispatched_at: datetime | None = None,
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
        last_dispatched_at=last_dispatched_at,
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
    assert acquired.lease_expires_at == (NOW + timedelta(seconds=30)).replace(
        tzinfo=None
    ) or acquired.lease_expires_at == NOW + timedelta(seconds=30)


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

    assert (
        repository.renew_lease(
            task.id,
            lease_token="invalid",
            now=NOW + timedelta(seconds=1),
            lease_seconds=10,
        )
        is False
    )

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
        prepared_output_sha256="2" * 64,
        duplicate_paragraphs_removed=1,
        phone_redaction_count=2,
        id_card_redaction_count=3,
        bank_card_redaction_count=4,
        email_redaction_count=5,
        ipv4_redaction_count=6,
        formatting_change_count=7,
        now=NOW + timedelta(seconds=1),
    )
    prepared = repository.get(task.id)
    assert prepared is not None
    assert prepared.processing_phase == MarkdownCleaningProcessingPhase.SAVING_PREPARED
    assert prepared.staging_path == "/staging/task.md"
    assert prepared.input_sha256 == "1" * 64
    assert prepared.prepared_output_sha256 == "2" * 64
    assert prepared.duplicate_paragraphs_removed == 1
    assert prepared.phone_redaction_count == 2
    assert prepared.id_card_redaction_count == 3
    assert prepared.bank_card_redaction_count == 4
    assert prepared.email_redaction_count == 5
    assert prepared.ipv4_redaction_count == 6
    assert prepared.formatting_change_count == 7

    assert repository.mark_publishing(
        task.id,
        lease_token=acquired.lease_token,
        now=NOW + timedelta(seconds=2),
    )

    assert repository.mark_succeeded(
        task.id,
        lease_token=acquired.lease_token,
        now=NOW + timedelta(seconds=3),
        output_sha256="2" * 64,
    )
    succeeded = repository.get(task.id)
    assert succeeded is not None
    assert succeeded.status is MarkdownCleaningTaskStatus.SUCCEEDED
    assert succeeded.progress_percent == 100
    assert succeeded.lease_token is None
    assert succeeded.processing_phase == MarkdownCleaningProcessingPhase.SUCCEEDED
    assert succeeded.prepared_output_sha256 == "2" * 64
    assert succeeded.output_sha256 == "2" * 64
    assert succeeded.published_at in (
        NOW + timedelta(seconds=3),
        NOW.replace(tzinfo=None) + timedelta(seconds=3),
    )
    assert succeeded.finished_at in (
        NOW + timedelta(seconds=3),
        NOW.replace(tzinfo=None) + timedelta(seconds=3),
    )


def test_mark_failed_while_publishing_keeps_recoverable_artifacts(
    session: Session,
) -> None:
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
        prepared_output_sha256="2" * 64,
        duplicate_paragraphs_removed=1,
        phone_redaction_count=2,
        id_card_redaction_count=3,
        bank_card_redaction_count=4,
        email_redaction_count=5,
        ipv4_redaction_count=6,
        formatting_change_count=7,
        now=NOW + timedelta(seconds=1),
    )
    assert repository.mark_publishing(
        task.id,
        lease_token=acquired.lease_token,
        now=NOW + timedelta(seconds=2),
    )

    assert repository.mark_failed(
        task.id,
        lease_token=acquired.lease_token,
        now=NOW + timedelta(seconds=2),
        error_code="FAILED",
        error_message="publish failed",
        processing_phase=MarkdownCleaningProcessingPhase.PUBLISHING_RESULT,
    )

    failed = repository.get(task.id)
    assert failed is not None
    assert failed.status is MarkdownCleaningTaskStatus.FAILED
    assert failed.processing_phase == MarkdownCleaningProcessingPhase.PUBLISHING_RESULT
    assert failed.staging_path == "/staging/task.md"
    assert failed.input_sha256 == "1" * 64
    assert failed.prepared_output_sha256 == "2" * 64
    assert failed.duplicate_paragraphs_removed == 1
    assert failed.phone_redaction_count == 2
    assert failed.id_card_redaction_count == 3
    assert failed.bank_card_redaction_count == 4
    assert failed.email_redaction_count == 5
    assert failed.ipv4_redaction_count == 6
    assert failed.formatting_change_count == 7
    assert failed.lease_token is None
    assert failed.finished_at in (
        NOW + timedelta(seconds=2),
        NOW.replace(tzinfo=None) + timedelta(seconds=2),
    )


def test_mark_publishing_requires_summary_counts(session: Session) -> None:
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
        prepared_output_sha256="2" * 64,
        duplicate_paragraphs_removed=1,
        phone_redaction_count=2,
        id_card_redaction_count=3,
        bank_card_redaction_count=4,
        email_redaction_count=5,
        ipv4_redaction_count=6,
        now=NOW + timedelta(seconds=1),
    )

    assert (
        repository.mark_publishing(
            task.id,
            lease_token=acquired.lease_token,
            now=NOW + timedelta(seconds=2),
        )
        is False
    )


def test_mark_publishing_rejects_wrong_processing_phase(session: Session) -> None:
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
        prepared_output_sha256="2" * 64,
        duplicate_paragraphs_removed=1,
        phone_redaction_count=2,
        id_card_redaction_count=3,
        bank_card_redaction_count=4,
        email_redaction_count=5,
        ipv4_redaction_count=6,
        formatting_change_count=7,
        now=NOW + timedelta(seconds=1),
    )
    assert repository.update_progress(
        task.id,
        lease_token=acquired.lease_token,
        progress_percent=20,
        processing_phase=MarkdownCleaningProcessingPhase.CLEANING,
        now=NOW + timedelta(seconds=2),
    )

    assert (
        repository.mark_publishing(
            task.id,
            lease_token=acquired.lease_token,
            now=NOW + timedelta(seconds=2),
        )
        is False
    )

    unchanged = repository.get(task.id)
    assert unchanged is not None
    assert unchanged.processing_phase == MarkdownCleaningProcessingPhase.CLEANING
    assert unchanged.lease_token == acquired.lease_token
    assert unchanged.staging_path == "/staging/task.md"
    assert unchanged.input_sha256 == "1" * 64
    assert unchanged.prepared_output_sha256 == "2" * 64
    assert unchanged.duplicate_paragraphs_removed == 1
    assert unchanged.phone_redaction_count == 2
    assert unchanged.id_card_redaction_count == 3
    assert unchanged.bank_card_redaction_count == 4
    assert unchanged.email_redaction_count == 5
    assert unchanged.ipv4_redaction_count == 6
    assert unchanged.formatting_change_count == 7


def test_mark_succeeded_requires_mark_publishing(session: Session) -> None:
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
        prepared_output_sha256="2" * 64,
        duplicate_paragraphs_removed=1,
        phone_redaction_count=2,
        id_card_redaction_count=3,
        bank_card_redaction_count=4,
        email_redaction_count=5,
        ipv4_redaction_count=6,
        formatting_change_count=7,
        now=NOW + timedelta(seconds=1),
    )

    assert (
        repository.mark_succeeded(
            task.id,
            lease_token=acquired.lease_token,
            now=NOW + timedelta(seconds=2),
            output_sha256="2" * 64,
        )
        is False
    )

    failed = repository.get(task.id)
    assert failed is not None
    assert failed.processing_phase == MarkdownCleaningProcessingPhase.SAVING_PREPARED


def test_mark_succeeded_rejects_without_prepared_artifacts(session: Session) -> None:
    task = make_task()
    session.add(task)
    session.commit()
    repository = MarkdownCleaningTaskRepository(session)
    acquired = repository.acquire_queued(task.id, now=NOW, lease_seconds=20)
    assert acquired is not None
    assert acquired.lease_token is not None

    assert (
        repository.mark_succeeded(
            task.id,
            lease_token=acquired.lease_token,
            now=NOW + timedelta(seconds=2),
            output_sha256="3" * 64,
        )
        is False
    )

    failed = repository.get(task.id)
    assert failed is not None
    assert failed.status is MarkdownCleaningTaskStatus.RUNNING
    assert failed.lease_token == acquired.lease_token


def test_mark_succeeded_rejects_output_digest_mismatch(session: Session) -> None:
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
        prepared_output_sha256="2" * 64,
        duplicate_paragraphs_removed=1,
        phone_redaction_count=2,
        id_card_redaction_count=3,
        bank_card_redaction_count=4,
        email_redaction_count=5,
        ipv4_redaction_count=6,
        formatting_change_count=7,
        now=NOW + timedelta(seconds=1),
    )
    assert repository.mark_publishing(
        task.id,
        lease_token=acquired.lease_token,
        now=NOW + timedelta(seconds=2),
    )

    assert (
        repository.mark_succeeded(
            task.id,
            lease_token=acquired.lease_token,
            now=NOW + timedelta(seconds=3),
            output_sha256="3" * 64,
        )
        is False
    )

    failed = repository.get(task.id)
    assert failed is not None
    assert failed.status is MarkdownCleaningTaskStatus.RUNNING
    assert failed.processing_phase == MarkdownCleaningProcessingPhase.PUBLISHING_RESULT


def test_mark_recovery_dispatched_is_throttled(session: Session) -> None:
    task = make_task(
        queued_at=NOW - timedelta(minutes=1),
        lease_expires_at=NOW - timedelta(minutes=1),
    )
    session.add(task)
    session.commit()

    repository = MarkdownCleaningTaskRepository(session)
    assert repository.mark_recovery_dispatched(
        task.id,
        now=NOW,
        queue_recovery_interval_seconds=30,
    )

    first_dispatched = repository.get(task.id)
    assert first_dispatched is not None
    assert first_dispatched.last_dispatched_at in (
        NOW,
        NOW.replace(tzinfo=None),
    )

    assert (
        repository.mark_recovery_dispatched(
            task.id,
            now=NOW + timedelta(seconds=5),
            queue_recovery_interval_seconds=30,
        )
        is False
    )

    assert (
        repository.mark_recovery_dispatched(
            task.id,
            now=NOW + timedelta(seconds=31),
            queue_recovery_interval_seconds=30,
        )
        is True
    )
    second_dispatched = repository.get(task.id)
    assert second_dispatched is not None
    assert second_dispatched.last_dispatched_at in (
        NOW + timedelta(seconds=31),
        NOW.replace(tzinfo=None) + timedelta(seconds=31),
    )


def test_recoverable_lists_and_active_count(session: Session) -> None:
    recoverable_queued = make_task(
        file_id="queued-stale",
        status=MarkdownCleaningTaskStatus.QUEUED,
        queued_at=NOW - timedelta(minutes=2),
        lease_expires_at=NOW - timedelta(minutes=1),
        last_dispatched_at=NOW - timedelta(minutes=1),
    )
    not_recoverable_recent = make_task(
        file_id="queued-not-ready",
        status=MarkdownCleaningTaskStatus.QUEUED,
        queued_at=NOW - timedelta(minutes=2),
        lease_expires_at=NOW - timedelta(minutes=1),
        last_dispatched_at=NOW - timedelta(seconds=10),
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
    running_without_lease = make_task(
        file_id="running-no-lease",
        status=MarkdownCleaningTaskStatus.RUNNING,
        lease_token=None,
        lease_expires_at=None,
    )
    session.add_all(
        [
            recoverable_queued,
            not_recoverable_recent,
            blocked_queued,
            running_active,
            running_expired,
            running_without_lease,
        ]
    )
    session.commit()

    repository = MarkdownCleaningTaskRepository(session)
    recovered = repository.list_recoverable_queued(
        now=NOW,
        queue_recovery_interval_seconds=30,
        limit=10,
    )
    assert len(recovered) == 1
    assert recovered[0].file_id == "queued-stale"

    expired_running = repository.list_recoverable_running(now=NOW, limit=10)
    assert [task.file_id for task in expired_running] == [
        "running-expired",
    ]

    assert repository.count_active_running(now=NOW) == 1


def test_naive_datetime_is_rejected_for_running_updates(session: Session) -> None:
    task = make_task()
    session.add(task)
    session.commit()
    repository = MarkdownCleaningTaskRepository(session)
    acquired = repository.acquire_queued(task.id, now=NOW, lease_seconds=20)
    assert acquired is not None
    assert acquired.lease_token is not None

    with pytest.raises(ValueError, match="时区"):
        repository.save_prepared(
            task.id,
            lease_token=acquired.lease_token,
            staging_path="/staging/task.md",
            input_sha256="1" * 64,
            prepared_output_sha256="2" * 64,
            now=datetime(2026, 8, 3, 8, 0),
        )
