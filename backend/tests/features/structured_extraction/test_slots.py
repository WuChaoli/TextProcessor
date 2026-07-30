import threading
import uuid
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.core.db import engine
from app.features.structured_extraction.models import (
    ExtractionTask,
    ExtractionTaskStatus,
    ProcessorSlot,
)
from app.features.structured_extraction.slots import (
    PROCESSOR_SLOT_QUARANTINE_EXPIRED,
    ProcessorSlotRepository,
)
from app.models import User


@pytest.fixture
def session() -> Generator[Session]:
    sqlite_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(sqlite_engine)
    with Session(sqlite_engine) as sqlite_session:
        yield sqlite_session


def test_acquire_is_idempotent_for_the_same_task(session: Session) -> None:
    repository = ProcessorSlotRepository(session)
    task_id = uuid.uuid4()
    now = datetime(2026, 7, 30, tzinfo=UTC)

    first = repository.acquire(
        task_id=task_id,
        processor_name="docling",
        max_in_flight=1,
        lease_duration=timedelta(seconds=30),
        now=now,
    )
    second = repository.acquire(
        task_id=task_id,
        processor_name="docling",
        max_in_flight=1,
        lease_duration=timedelta(seconds=30),
        now=now + timedelta(seconds=1),
    )

    assert first is not None
    assert second is not None
    assert second.id == first.id
    assert session.exec(select(ProcessorSlot)).all() == [first]


def test_refresh_extends_an_active_slot_lease(session: Session) -> None:
    repository = ProcessorSlotRepository(session)
    task_id = uuid.uuid4()
    acquired_at = datetime(2026, 7, 30, tzinfo=UTC)
    acquired = repository.acquire(
        task_id=task_id,
        processor_name="mineru",
        max_in_flight=1,
        lease_duration=timedelta(seconds=30),
        now=acquired_at,
    )

    refreshed = repository.refresh(
        task_id,
        lease_duration=timedelta(seconds=45),
        now=acquired_at + timedelta(seconds=10),
    )

    assert acquired is not None
    assert refreshed is not None
    assert refreshed.acquired_at.replace(tzinfo=UTC) == acquired_at
    assert refreshed.lease_expires_at.replace(tzinfo=UTC) == acquired_at + timedelta(
        seconds=55
    )


def test_quarantined_slot_still_consumes_capacity_until_reaped(
    session: Session,
) -> None:
    repository = ProcessorSlotRepository(session)
    first_task_id = uuid.uuid4()
    now = datetime(2026, 7, 30, tzinfo=UTC)
    acquired = repository.acquire(
        task_id=first_task_id,
        processor_name="docling",
        max_in_flight=1,
        lease_duration=timedelta(seconds=30),
        now=now,
    )

    quarantined = repository.quarantine(first_task_id, now=now + timedelta(seconds=5))
    unavailable = repository.acquire(
        task_id=uuid.uuid4(),
        processor_name="docling",
        max_in_flight=1,
        lease_duration=timedelta(seconds=30),
        now=now + timedelta(seconds=6),
    )
    alerts = repository.reap(
        quarantine_grace=timedelta(seconds=10),
        now=now + timedelta(seconds=16),
    )
    available = repository.acquire(
        task_id=uuid.uuid4(),
        processor_name="docling",
        max_in_flight=1,
        lease_duration=timedelta(seconds=30),
        now=now + timedelta(seconds=16),
    )

    assert acquired is not None
    assert quarantined is not None
    assert quarantined.state == "quarantined"
    assert unavailable is None
    assert [(alert.event, alert.task_id) for alert in alerts] == [
        (PROCESSOR_SLOT_QUARANTINE_EXPIRED, first_task_id)
    ]
    assert available is not None


def test_release_makes_a_terminal_task_slot_available(session: Session) -> None:
    repository = ProcessorSlotRepository(session)
    task_id = uuid.uuid4()
    now = datetime(2026, 7, 30, tzinfo=UTC)
    acquired = repository.acquire(
        task_id=task_id,
        processor_name="mineru",
        max_in_flight=1,
        lease_duration=timedelta(seconds=30),
        now=now,
    )

    released = repository.release(task_id)
    replacement = repository.acquire(
        task_id=uuid.uuid4(),
        processor_name="mineru",
        max_in_flight=1,
        lease_duration=timedelta(seconds=30),
        now=now,
    )

    assert acquired is not None
    assert released is True
    assert replacement is not None


@pytest.mark.real_integration
def test_postgresql_capacity_lock_allows_only_one_concurrent_acquisition() -> None:
    assert engine.dialect.name == "postgresql"
    processor_name = f"slot-{uuid.uuid4().hex[:16]}"
    task_ids = (uuid.uuid4(), uuid.uuid4())
    caller_id = uuid.uuid4()
    now = datetime(2026, 7, 30, tzinfo=UTC)
    start = threading.Barrier(2)

    with Session(engine) as setup_session:
        setup_session.add(
            User(
                id=caller_id,
                email=f"processor-slot-{caller_id}@example.com",
                hashed_password="not-used",
            )
        )
        setup_session.commit()
        for task_id in task_ids:
            setup_session.add(
                ExtractionTask(
                    id=task_id,
                    caller_id=caller_id,
                    session_id=str(task_id),
                    file_id="processor-slot",
                    request_fingerprint="a" * 64,
                    file_storage_path="/test/input.txt",
                    selected_input_type="local",
                    target_path=f"/test/{task_id}.md",
                    status=ExtractionTaskStatus.RUNNING,
                )
            )
        setup_session.commit()

    def acquire(task_id: uuid.UUID) -> ProcessorSlot | None:
        with Session(engine) as slot_session:
            start.wait(timeout=5)
            return ProcessorSlotRepository(slot_session).acquire(
                task_id=task_id,
                processor_name=processor_name,
                max_in_flight=1,
                lease_duration=timedelta(seconds=30),
                now=now,
            )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            acquired = list(executor.map(acquire, task_ids))
    finally:
        with Session(engine) as cleanup_session:
            for task_id in task_ids:
                slot = cleanup_session.exec(
                    select(ProcessorSlot).where(ProcessorSlot.task_id == task_id)
                ).one_or_none()
                if slot is not None:
                    cleanup_session.delete(slot)
                task = cleanup_session.get(ExtractionTask, task_id)
                if task is not None:
                    cleanup_session.delete(task)
            user = cleanup_session.get(User, caller_id)
            if user is not None:
                cleanup_session.delete(user)
            cleanup_session.commit()

    assert sum(slot is not None for slot in acquired) == 1
