import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.features.structured_extraction.dispatcher import (
    CeleryExtractionTaskDispatcher,
)
from app.features.structured_extraction.errors import (
    ExtractionDomainError,
    ExtractionErrorCode,
)
from app.features.structured_extraction.models import ExtractionTaskStatus
from app.features.structured_extraction.repository import ExtractionTaskRepository
from app.features.structured_extraction.request_policy import RequestPolicy
from app.features.structured_extraction.schemas import ExtractionTaskCreate
from app.features.structured_extraction.service import ExtractionTaskService
from app.models import User  # noqa: F401


class FakeDispatcher:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.task_ids: list[uuid.UUID] = []
        self.failure = failure

    def enqueue_submit(self, task_id: uuid.UUID) -> None:
        self.task_ids.append(task_id)
        if self.failure:
            raise self.failure


class FakeCelery:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def send_task(self, name: str, *, kwargs: dict[str, Any]) -> None:
        self.calls.append((name, kwargs))


@pytest.fixture
def session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def make_request_and_policy(
    tmp_path: Path,
) -> tuple[ExtractionTaskCreate, RequestPolicy]:
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    input_root.mkdir()
    output_root.mkdir()
    source = input_root / "sample.txt"
    source.write_text("hello", encoding="utf-8")
    request = ExtractionTaskCreate(
        sessionId="session-1",
        fileId="file-1",
        fileStoragePath=str(source),
        targetPath=str(output_root / "sample.md"),
    )
    policy = RequestPolicy(
        input_roots=(input_root,),
        output_roots=(output_root,),
        allowed_http_hosts=(),
        allowed_http_cidrs=(),
        max_input_bytes=1024,
    )
    return request, policy


def test_create_queues_new_task_once(
    session: Session,
    tmp_path: Path,
) -> None:
    caller_id = uuid.uuid4()
    request, policy = make_request_and_policy(tmp_path)
    dispatcher = FakeDispatcher()
    service = ExtractionTaskService(
        ExtractionTaskRepository(session),
        policy,
        dispatcher,
    )

    first = service.create_task(caller_id, request)
    second = service.create_task(caller_id, request)

    assert first.id == second.id
    assert first.status is ExtractionTaskStatus.QUEUED
    assert dispatcher.task_ids == [first.id]


def test_enqueue_failure_marks_task_failed_and_hides_broker_error(
    session: Session,
    tmp_path: Path,
) -> None:
    caller_id = uuid.uuid4()
    request, policy = make_request_and_policy(tmp_path)
    repository = ExtractionTaskRepository(session)
    service = ExtractionTaskService(
        repository,
        policy,
        FakeDispatcher(failure=RuntimeError("redis-secret-detail")),
    )

    with pytest.raises(ExtractionDomainError) as raised:
        service.create_task(caller_id, request)

    assert raised.value.code is ExtractionErrorCode.QUEUE_SUBMISSION_FAILED
    assert "redis-secret-detail" not in raised.value.safe_message
    task = repository.get_by_key(caller_id, "session-1", "file-1")
    assert task is not None
    assert task.status is ExtractionTaskStatus.FAILED
    assert task.error_code == ExtractionErrorCode.QUEUE_SUBMISSION_FAILED
    assert task.error_message == "任务提交失败"
    assert task.finished_at is not None


def test_get_task_is_scoped_to_caller(
    session: Session,
    tmp_path: Path,
) -> None:
    caller_id = uuid.uuid4()
    request, policy = make_request_and_policy(tmp_path)
    service = ExtractionTaskService(
        ExtractionTaskRepository(session),
        policy,
        FakeDispatcher(),
    )
    task = service.create_task(caller_id, request)

    assert service.get_task(caller_id, task.id) is not None
    assert service.get_task(uuid.uuid4(), task.id) is None


def test_celery_dispatcher_sends_identity_only() -> None:
    celery = FakeCelery()
    dispatcher = CeleryExtractionTaskDispatcher(celery)  # type: ignore[arg-type]
    task_id = uuid.uuid4()

    dispatcher.enqueue_submit(task_id)

    assert celery.calls == [
        (
            "structured_extraction.submit",
            {
                "task_id": str(task_id),
                "task_type": "structured_extraction",
                "schema_version": 1,
            },
        )
    ]
