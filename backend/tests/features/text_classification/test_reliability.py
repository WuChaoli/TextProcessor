import uuid
from datetime import timedelta

import httpx
import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.features.text_classification import celery_tasks
from app.features.text_classification.celery_tasks import (
    RetryableClassificationError,
    execute,
    recover,
)
from app.features.text_classification.input_preparer import (
    PreparedClassificationInput,
)
from app.features.text_classification.models import ClassificationTask, utc_now
from app.features.text_classification.repository import ClassificationTaskRepository
from app.tasking.state import TaskStatus


class RecordingDispatcher:
    def __init__(self) -> None:
        self.task_ids: list[uuid.UUID] = []

    def enqueue(self, task_id: uuid.UUID) -> None:
        self.task_ids.append(task_id)


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


def make_task(status: TaskStatus, **fields: object) -> ClassificationTask:
    return ClassificationTask(
        caller_id=uuid.uuid4(),
        session_id=f"session-{uuid.uuid4()}",
        file_id="file-1",
        request_fingerprint="a" * 64,
        input_uri="file:///input/a.txt",
        status=status,
        **fields,
    )


def test_claim_rejects_duplicate_message_while_lease_is_live(session: Session) -> None:
    task = make_task(
        TaskStatus.RUNNING,
        attempt_count=1,
        lease_expires_at=utc_now() + timedelta(minutes=1),
    )
    session.add(task)
    session.commit()

    claimed = ClassificationTaskRepository(session).claim_for_execution(
        task.id,
        now=utc_now(),
        lease_seconds=30,
    )

    assert claimed is None
    session.refresh(task)
    assert task.attempt_count == 1


def test_recovery_dispatches_lost_queued_and_expired_running_once(
    session: Session,
) -> None:
    now = utc_now()
    queued = make_task(TaskStatus.QUEUED, queued_at=now - timedelta(minutes=2))
    running = make_task(
        TaskStatus.RUNNING,
        attempt_count=1,
        lease_expires_at=now - timedelta(seconds=1),
    )
    healthy = make_task(
        TaskStatus.RUNNING,
        attempt_count=1,
        lease_expires_at=now + timedelta(minutes=1),
    )
    session.add_all((queued, running, healthy))
    session.commit()
    dispatcher = RecordingDispatcher()

    recovered = recover(session, dispatcher, now=now, limit=10)

    assert recovered == 2
    assert dispatcher.task_ids == [queued.id, running.id]
    session.refresh(queued)
    session.refresh(running)
    assert queued.last_dispatched_at is not None
    assert running.last_dispatched_at is not None


def test_expired_running_task_stops_after_finite_attempts(session: Session) -> None:
    task = make_task(
        TaskStatus.RUNNING,
        attempt_count=3,
        max_attempts=3,
        lease_expires_at=utc_now() - timedelta(seconds=1),
    )
    session.add(task)
    session.commit()

    recovered = recover(session, RecordingDispatcher(), now=utc_now(), limit=10)

    assert recovered == 0
    session.refresh(task)
    assert task.status is TaskStatus.FAILED
    assert task.error_code == "ATTEMPTS_EXHAUSTED"


def test_duplicate_delivery_persists_one_result(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = make_task(TaskStatus.QUEUED)
    session.add(task)
    session.commit()
    calls: list[str] = []

    class FakePreparer:
        def __init__(self, **_kwargs: object) -> None: pass

        def prepare(self, task_id: str, _uri: str) -> PreparedClassificationInput:
            return PreparedClassificationInput(f"file:///staging/{task_id}/input.txt", "b" * 64, 12)

    class FakeClient:
        def __init__(self, **_kwargs: object) -> None: pass

        def classify(self, request_id: str, _uri: str) -> dict[str, object]:
            calls.append(request_id)
            return {"schemaVersion": "1", "requestId": request_id, "tags": ["a", "b", "c", "d"]}

    monkeypatch.setattr(celery_tasks, "ClassificationInputPreparer", FakePreparer)
    monkeypatch.setattr(celery_tasks, "ClassificationClient", FakeClient)

    execute(session, task.id)
    execute(session, task.id)

    session.refresh(task)
    assert task.status is TaskStatus.SUCCEEDED
    assert task.attempt_count == 1
    assert calls == [str(task.id)]


def test_transient_failure_is_retried_only_to_max_attempts(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = make_task(TaskStatus.QUEUED, max_attempts=3)
    session.add(task)
    session.commit()

    class FakePreparer:
        def __init__(self, **_kwargs: object) -> None: pass

        def prepare(self, task_id: str, _uri: str) -> PreparedClassificationInput:
            return PreparedClassificationInput(f"file:///staging/{task_id}/input.txt", "b" * 64, 12)

    class FailingClient:
        def __init__(self, **_kwargs: object) -> None: pass

        def classify(self, _request_id: str, _uri: str) -> dict[str, object]:
            raise httpx.ConnectError("unavailable")

    monkeypatch.setattr(celery_tasks, "ClassificationInputPreparer", FakePreparer)
    monkeypatch.setattr(celery_tasks, "ClassificationClient", FailingClient)

    with pytest.raises(RetryableClassificationError):
        execute(session, task.id)
    with pytest.raises(RetryableClassificationError):
        execute(session, task.id)
    execute(session, task.id)

    session.refresh(task)
    assert task.status is TaskStatus.FAILED
    assert task.attempt_count == 3
    assert task.error_code == "CLASSIFICATION_FAILED"
