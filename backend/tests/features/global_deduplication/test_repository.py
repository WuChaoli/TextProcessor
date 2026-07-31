import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.core.db import engine
from app.features.global_deduplication.repository import (
    GlobalDeduplicationTaskRepository,
)
from app.features.global_deduplication.state_machine import (
    GlobalDeduplicationTaskStatus,
)
from app.features.global_deduplication.task_models import GlobalDeduplicationTask
from app.models import User

NOW = datetime(2026, 7, 31, 8, 0, tzinfo=UTC)


@pytest.fixture
def session() -> Session:
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
    caller_id: uuid.UUID | None = None,
    session_id: str = "session-1",
    status: GlobalDeduplicationTaskStatus = GlobalDeduplicationTaskStatus.QUEUED,
) -> GlobalDeduplicationTask:
    return GlobalDeduplicationTask(
        caller_id=caller_id or uuid.uuid4(),
        session_id=session_id,
        request_fingerprint="a" * 64,
        input_json_path="/data/input.json",
        target_path="/data/output.json",
        status=status,
        queued_at=NOW,
        max_attempts=3,
    )


def test_caller_and_session_are_unique(session: Session) -> None:
    caller_id = uuid.uuid4()
    session.add(make_task(caller_id=caller_id))
    session.commit()
    session.add(make_task(caller_id=caller_id))

    with pytest.raises(IntegrityError):
        session.commit()


def test_submit_lease_can_only_be_acquired_once(session: Session) -> None:
    task = make_task()
    session.add(task)
    session.commit()
    repository = GlobalDeduplicationTaskRepository(session)

    first = repository.acquire_submit(task.id, now=NOW, lease_seconds=30)
    second = repository.acquire_submit(task.id, now=NOW, lease_seconds=30)

    assert first is not None
    assert first.status is GlobalDeduplicationTaskStatus.RUNNING
    assert first.attempt_count == 1
    assert second is None


def test_expired_submit_lease_without_external_job_is_reacquired(
    session: Session,
) -> None:
    task = make_task(status=GlobalDeduplicationTaskStatus.RUNNING)
    task.lease_expires_at = NOW - timedelta(seconds=1)
    task.attempt_count = 1
    session.add(task)
    session.commit()
    repository = GlobalDeduplicationTaskRepository(session)

    acquired = repository.acquire_submit(task.id, now=NOW, lease_seconds=30)

    assert acquired is not None
    assert acquired.attempt_count == 2


def test_only_one_poll_worker_acquires_lease(session: Session) -> None:
    task = make_task(status=GlobalDeduplicationTaskStatus.RUNNING)
    task.external_job_id = uuid.uuid7()
    task.external_profile = "text_exact_minhash_v1"
    task.next_poll_at = NOW
    session.add(task)
    session.commit()
    repository = GlobalDeduplicationTaskRepository(session)

    first = repository.acquire_poll(task.id, now=NOW, lease_seconds=30)
    second = repository.acquire_poll(task.id, now=NOW, lease_seconds=30)

    assert first is not None
    assert second is None


def test_repository_persists_prepared_and_external_metadata(
    session: Session,
) -> None:
    task = make_task(status=GlobalDeduplicationTaskStatus.RUNNING)
    session.add(task)
    session.commit()
    repository = GlobalDeduplicationTaskRepository(session)
    job_id = uuid.uuid7()

    assert repository.save_prepared_input(
        task.id,
        staging_path="/staging/task",
        input_manifest_sha256="1" * 64,
        input_jsonl_sha256="2" * 64,
        mapping_sha256="3" * 64,
        progress_total=2,
    )
    assert repository.save_external_job(
        task.id,
        external_job_id=job_id,
        external_profile="text_exact_minhash_v1",
        next_poll_at=NOW,
        processing_deadline=NOW + timedelta(hours=1),
    )

    saved = repository.get(task.id)
    assert saved is not None
    assert saved.external_job_id == job_id
    assert saved.input_jsonl_sha256 == "2" * 64
    assert saved.processing_phase == "deduplicating"


def test_recovery_lists_only_due_polls(session: Session) -> None:
    due = make_task(
        session_id="due",
        status=GlobalDeduplicationTaskStatus.RUNNING,
    )
    due.external_job_id = uuid.uuid7()
    due.next_poll_at = NOW
    future = make_task(
        session_id="future",
        status=GlobalDeduplicationTaskStatus.RUNNING,
    )
    future.external_job_id = uuid.uuid7()
    future.next_poll_at = NOW + timedelta(minutes=1)
    session.add(due)
    session.add(future)
    session.commit()

    tasks = GlobalDeduplicationTaskRepository(session).list_due_polls(
        now=NOW,
        limit=10,
    )

    assert [task.id for task in tasks] == [due.id]


def test_postgresql_poll_lease_is_atomic(db: Session) -> None:
    if db.get_bind().dialect.name != "postgresql":
        pytest.skip("PostgreSQL is required")
    caller = db.exec(select(User)).first()
    assert caller is not None
    task = make_task(
        caller_id=caller.id,
        session_id=f"postgres-{uuid.uuid7()}",
        status=GlobalDeduplicationTaskStatus.RUNNING,
    )
    task.external_job_id = uuid.uuid7()
    task.next_poll_at = NOW
    db.add(task)
    db.commit()
    repository = GlobalDeduplicationTaskRepository(db)

    first = repository.acquire_poll(task.id, now=NOW, lease_seconds=30)
    second = repository.acquire_poll(task.id, now=NOW, lease_seconds=30)

    assert first is not None
    assert second is None


def test_postgresql_concurrent_create_converges_to_one_task(
    db: Session,
) -> None:
    if db.get_bind().dialect.name != "postgresql":
        pytest.skip("PostgreSQL is required")
    caller = db.exec(select(User)).first()
    assert caller is not None
    session_id = f"concurrent-{uuid.uuid7()}"

    def create() -> tuple[uuid.UUID, bool]:
        with Session(engine) as session:
            task, created = GlobalDeduplicationTaskRepository(
                session
            ).create_or_get(
                caller_id=caller.id,
                session_id=session_id,
                input_json_path="/data/input.json",
                target_path="/data/output.json",
            )
            return task.id, created

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: create(), range(2)))

    assert len({task_id for task_id, _created in results}) == 1
    assert sum(created for _task_id, created in results) == 1
    task_id = results[0][0]
    saved = db.get(GlobalDeduplicationTask, task_id)
    assert saved is not None
    db.delete(saved)
    db.commit()
