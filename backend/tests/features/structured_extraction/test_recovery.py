import uuid
from collections.abc import Generator
from datetime import timedelta
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.core.config import ExtractionWorkerSettings
from app.features.structured_extraction.adapters.protocol import (
    ExternalProcessorAdapter,
)
from app.features.structured_extraction.models import (
    ExtractionTask,
    ExtractionTaskStatus,
    ProcessorSlot,
    get_datetime_utc,
)
from app.features.structured_extraction.orchestration import ExtractionOrchestrator
from app.features.structured_extraction.slots import ProcessorSlotRepository
from app.features.structured_extraction.worker_models import ProcessorName
from app.models import User  # noqa: F401


class RecordingScheduler:
    def __init__(self) -> None:
        self.submit_calls: list[tuple[uuid.UUID, int]] = []
        self.poll_calls: list[tuple[uuid.UUID, int]] = []

    def enqueue_submit(self, task_id: uuid.UUID, *, countdown: int) -> None:
        self.submit_calls.append((task_id, countdown))

    def enqueue_poll(self, task_id: uuid.UUID, *, countdown: int) -> None:
        self.poll_calls.append((task_id, countdown))


@pytest.fixture
def session() -> Generator[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as value:
        yield value


def make_task(
    *,
    status: ExtractionTaskStatus,
    **fields: object,
) -> ExtractionTask:
    return ExtractionTask(
        caller_id=uuid.uuid4(),
        session_id="session-recovery",
        file_id=f"file-{uuid.uuid4()}",
        request_fingerprint="b" * 64,
        file_storage_path="/input/report.pdf",
        selected_input_type="local",
        target_path=f"/output/{uuid.uuid4()}.md",
        status=status,
        **fields,
    )


def make_orchestrator(
    session: Session,
    tmp_path: Path,
    scheduler: RecordingScheduler,
    *,
    slot_quarantine_grace_seconds: int = 300,
) -> ExtractionOrchestrator:
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    input_root.mkdir(exist_ok=True)
    output_root.mkdir(exist_ok=True)

    def unavailable_adapter(_processor: ProcessorName) -> ExternalProcessorAdapter:
        raise AssertionError("recovery must only schedule messages")

    return ExtractionOrchestrator(
        session,
        worker_settings=ExtractionWorkerSettings(
            staging_root=tmp_path / "staging",
            output_roots=(output_root,),
            recovery_batch_size=1,
            slot_quarantine_grace_seconds=slot_quarantine_grace_seconds,
        ),
        input_roots=(input_root,),
        max_input_bytes=1024,
        scheduler=scheduler,
        adapter_factory=unavailable_adapter,
    )


def test_recover_redispatches_lost_queued_message_in_stable_limited_batch(
    session: Session,
    tmp_path: Path,
) -> None:
    now = get_datetime_utc()
    first = make_task(
        status=ExtractionTaskStatus.QUEUED,
        queued_at=now - timedelta(minutes=2),
    )
    second = make_task(
        status=ExtractionTaskStatus.QUEUED,
        queued_at=now - timedelta(minutes=1),
    )
    session.add_all((first, second))
    session.commit()
    scheduler = RecordingScheduler()

    recovered = make_orchestrator(session, tmp_path, scheduler).recover(now=now)

    session.refresh(first)
    session.refresh(second)
    assert recovered == 1
    assert scheduler.submit_calls == [(first.id, 0)]
    assert first.last_dispatched_at is not None
    assert second.last_dispatched_at is None


def test_recover_reschedules_running_task_with_expired_poll_lease(
    session: Session,
    tmp_path: Path,
) -> None:
    now = get_datetime_utc()
    task = make_task(
        status=ExtractionTaskStatus.RUNNING,
        processor_name=ProcessorName.MINERU,
        external_task_id="mineru-1",
        next_poll_at=now - timedelta(seconds=1),
        poll_lease_expires_at=now - timedelta(seconds=1),
        processing_deadline=now + timedelta(minutes=5),
    )
    session.add(task)
    session.commit()
    scheduler = RecordingScheduler()

    recovered = make_orchestrator(session, tmp_path, scheduler).recover(now=now)

    assert recovered == 1
    assert scheduler.poll_calls == [(task.id, 0)]


def test_recover_does_not_duplicate_poll_with_valid_lease_or_terminal_task(
    session: Session,
    tmp_path: Path,
) -> None:
    now = get_datetime_utc()
    leased = make_task(
        status=ExtractionTaskStatus.RUNNING,
        processor_name=ProcessorName.MINERU,
        external_task_id="mineru-1",
        next_poll_at=now - timedelta(seconds=1),
        poll_lease_expires_at=now + timedelta(seconds=20),
        processing_deadline=now + timedelta(minutes=5),
    )
    terminal = make_task(
        status=ExtractionTaskStatus.FAILED,
        processor_name=ProcessorName.MINERU,
        external_task_id="mineru-2",
        next_poll_at=now - timedelta(seconds=1),
    )
    session.add_all((leased, terminal))
    session.commit()
    scheduler = RecordingScheduler()

    recovered = make_orchestrator(session, tmp_path, scheduler).recover(now=now)

    assert recovered == 0
    assert scheduler.submit_calls == []
    assert scheduler.poll_calls == []


def test_recover_releases_slot_orphaned_by_terminal_task(
    session: Session,
    tmp_path: Path,
) -> None:
    task = make_task(
        status=ExtractionTaskStatus.SUCCEEDED,
        processor_name=ProcessorName.MINERU,
    )
    session.add(task)
    session.commit()
    slots = ProcessorSlotRepository(session)
    assert (
        slots.acquire(
            task_id=task.id,
            processor_name=ProcessorName.MINERU,
            max_in_flight=1,
            lease_duration=timedelta(seconds=60),
        )
        is not None
    )
    scheduler = RecordingScheduler()

    recovered = make_orchestrator(session, tmp_path, scheduler).recover(
        now=get_datetime_utc()
    )

    assert recovered == 1
    assert (
        session.exec(
            select(ProcessorSlot).where(ProcessorSlot.task_id == task.id)
        ).one_or_none()
        is None
    )


def test_recover_reaps_quarantined_slot_after_configured_grace(
    session: Session,
    tmp_path: Path,
) -> None:
    now = get_datetime_utc()
    task = make_task(
        status=ExtractionTaskStatus.FAILED,
        processor_name=ProcessorName.MINERU,
    )
    session.add(task)
    session.commit()
    slots = ProcessorSlotRepository(session)
    assert (
        slots.acquire(
            task_id=task.id,
            processor_name=ProcessorName.MINERU,
            max_in_flight=1,
            lease_duration=timedelta(seconds=60),
            now=now - timedelta(seconds=5),
        )
        is not None
    )
    assert slots.quarantine(task.id, now=now - timedelta(seconds=1)) is not None
    scheduler = RecordingScheduler()

    recovered = make_orchestrator(
        session,
        tmp_path,
        scheduler,
        slot_quarantine_grace_seconds=0,
    ).recover(now=now)

    assert recovered == 1
    assert (
        session.exec(
            select(ProcessorSlot).where(ProcessorSlot.task_id == task.id)
        ).one_or_none()
        is None
    )
