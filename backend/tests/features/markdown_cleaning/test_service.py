import threading
import uuid
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app import crud
from app.core.db import engine
from app.features.markdown_cleaning.api_errors import (
    MarkdownCleaningApiErrorCode,
    MarkdownCleaningDomainError,
)
from app.features.markdown_cleaning.repository import (
    MarkdownCleaningTaskRepository,
)
from app.features.markdown_cleaning.request_policy import (
    MarkdownCleaningRequestPolicy,
)
from app.features.markdown_cleaning.schemas import MarkdownCleaningTaskCreate
from app.features.markdown_cleaning.service import MarkdownCleaningTaskService
from app.features.markdown_cleaning.state_machine import (
    MarkdownCleaningTaskStatus,
)
from app.models import UserCreate


@pytest.fixture(scope="session", autouse=True)
def db() -> Generator[None, None, None]:
    yield


@dataclass
class Dispatcher:
    fail: bool = False
    task_ids: list[uuid.UUID] = field(default_factory=list)

    def enqueue_execute(self, task_id: uuid.UUID) -> None:
        if self.fail:
            raise RuntimeError("broker unavailable: redis-secret")
        self.task_ids.append(task_id)


@pytest.fixture
def session() -> Generator[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session_:
        yield session_


def build_service(
    tmp_path: Path,
    *,
    session: Session,
    dispatcher: Dispatcher,
) -> tuple[MarkdownCleaningTaskService, MarkdownCleaningTaskCreate]:
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    input_root.mkdir()
    output_root.mkdir()
    source = input_root / "source.md"
    source.write_text("raw", encoding="utf-8")
    target = output_root / "result.md"
    request = MarkdownCleaningTaskCreate(
        sessionId="session-1",
        fileId="11",
        fileStoragePath=str(source),
        targetPath=str(target),
    )
    return (
        MarkdownCleaningTaskService(
            MarkdownCleaningTaskRepository(session),
            MarkdownCleaningRequestPolicy(
                input_roots=(input_root,),
                output_roots=(output_root,),
                allowed_http_hosts=(),
                allowed_http_cidrs=(),
            ),
            dispatcher,
        ),
        request,
    )


def _create_test_user() -> uuid.UUID:
    email = f"task4-test-{uuid.uuid7()}@example.com"
    with Session(engine) as db:
        SQLModel.metadata.create_all(db.get_bind())
        user = crud.create_user(
            session=db,
            user_create=UserCreate(email=email, password="complexPass123"),
        )
        return user.id


def _skip_if_postgres_unreachable() -> None:
    try:
        with engine.connect() as db:
            db.exec_driver_sql("SELECT 1")
    except OperationalError as error:
        pytest.skip(f"PostgreSQL unavailable: {error}")


@dataclass
class BlockingDispatcher:
    blocked_event: threading.Event = field(default_factory=threading.Event)
    release_event: threading.Event = field(default_factory=threading.Event)
    task_ids: list[uuid.UUID] = field(default_factory=list)
    fail_first: bool = True
    _first_seen: bool = field(default=False, init=False)

    def enqueue_execute(self, task_id: uuid.UUID) -> None:
        self.task_ids.append(task_id)
        if not self._first_seen:
            self._first_seen = True
            self.blocked_event.set()
            assert self.release_event.wait(timeout=8.0)
            if self.fail_first:
                raise RuntimeError("redis-secret: broker blocked")

        else:
            return


def test_create_is_idempotent_and_dispatches_once(
    session: Session,
    tmp_path: Path,
) -> None:
    dispatcher = Dispatcher()
    service, request = build_service(
        tmp_path,
        session=session,
        dispatcher=dispatcher,
    )
    caller_id = uuid.uuid7()

    first = service.create_task(caller_id, request)
    repeated = service.create_task(caller_id, request)

    assert first.id == repeated.id
    assert first.status is MarkdownCleaningTaskStatus.QUEUED
    saved = service._repository.get(first.id)
    assert saved is not None
    assert saved.last_dispatched_at is not None
    assert saved.queued_at is not None
    assert saved.processing_phase == "validating_input"
    assert saved.status is MarkdownCleaningTaskStatus.QUEUED
    assert dispatcher.task_ids == [first.id]


def test_same_session_with_different_path_conflicts(
    session: Session,
    tmp_path: Path,
) -> None:
    dispatcher = Dispatcher()
    service, request = build_service(
        tmp_path,
        session=session,
        dispatcher=dispatcher,
    )
    caller_id = uuid.uuid7()
    service.create_task(caller_id, request)

    with pytest.raises(MarkdownCleaningDomainError) as error:
        service.create_task(
            caller_id,
            MarkdownCleaningTaskCreate(
                sessionId="session-1",
                fileId="11",
                fileStoragePath=str(tmp_path / "input" / "source.md"),
                targetPath=str(tmp_path / "output" / "changed.md"),
            ),
        )

    assert error.value.code is MarkdownCleaningApiErrorCode.IDEMPOTENCY_CONFLICT
    assert error.value.http_status == 409


def test_queue_failure_is_persisted_and_returns_503(
    session: Session,
    tmp_path: Path,
) -> None:
    dispatcher = Dispatcher(fail=True)
    service, request = build_service(
        tmp_path,
        session=session,
        dispatcher=dispatcher,
    )
    caller_id = uuid.uuid7()

    with pytest.raises(MarkdownCleaningDomainError) as error:
        service.create_task(caller_id, request)

    assert error.value.code is MarkdownCleaningApiErrorCode.QUEUE_SUBMISSION_FAILED
    assert error.value.http_status == 503
    saved = service._repository.get_by_key(caller_id, "session-1", "11")
    assert saved is not None
    assert saved.status is MarkdownCleaningTaskStatus.FAILED
    assert saved.error_code == MarkdownCleaningApiErrorCode.QUEUE_SUBMISSION_FAILED
    assert saved.error_message == "任务提交失败"
    assert "redis-secret" not in (saved.error_message or "")


def test_queue_failure_replay_returns_same_safe_503_and_preserves_error(
    session: Session,
    tmp_path: Path,
) -> None:
    dispatcher = Dispatcher(fail=True)
    service, request = build_service(
        tmp_path,
        session=session,
        dispatcher=dispatcher,
    )
    caller_id = uuid.uuid7()
    with pytest.raises(MarkdownCleaningDomainError) as first:
        service.create_task(caller_id, request)

    assert first.value.code is MarkdownCleaningApiErrorCode.QUEUE_SUBMISSION_FAILED
    assert first.value.http_status == 503

    dispatcher.fail = False
    with pytest.raises(MarkdownCleaningDomainError) as replayed:
        service.create_task(caller_id, request)

    assert replayed.value.code is MarkdownCleaningApiErrorCode.QUEUE_SUBMISSION_FAILED
    assert replayed.value.http_status == 503
    assert replayed.value.safe_message == first.value.safe_message
    assert "redis-secret" not in replayed.value.safe_message
    assert dispatcher.task_ids == []


def test_concurrent_replay_waits_for_lock_and_returns_safe_503(
    tmp_path: Path,
) -> None:
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL is required for advisory-lock concurrency test")
    _skip_if_postgres_unreachable()

    caller_id = _create_test_user()
    dispatcher = BlockingDispatcher()

    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    input_root.mkdir(exist_ok=True)
    output_root.mkdir(exist_ok=True)
    source = input_root / "source.md"
    source.write_text("raw", encoding="utf-8")
    target = output_root / "result.md"
    markdown_request = MarkdownCleaningTaskCreate(
        sessionId="session-1",
        fileId="11",
        fileStoragePath=str(source),
        targetPath=str(target),
    )
    policy = MarkdownCleaningRequestPolicy(
        input_roots=(input_root,),
        output_roots=(output_root,),
        allowed_http_hosts=(),
        allowed_http_cidrs=(),
    )

    def create_once() -> MarkdownCleaningDomainError | None:
        with Session(engine) as pg_session:
            service = MarkdownCleaningTaskService(
                MarkdownCleaningTaskRepository(pg_session),
                policy,
                dispatcher,
            )
            try:
                service.create_task(caller_id, markdown_request)
                return None
            except MarkdownCleaningDomainError as error:
                return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(create_once)
        assert dispatcher.blocked_event.wait(timeout=5.0)
        second_future = executor.submit(create_once)

        dispatcher.release_event.set()
        first_result = first_future.result(timeout=10.0)
        second_result = second_future.result(timeout=10.0)

    assert isinstance(first_result, MarkdownCleaningDomainError)
    assert first_result.code is MarkdownCleaningApiErrorCode.QUEUE_SUBMISSION_FAILED
    assert first_result.http_status == 503
    assert isinstance(second_result, MarkdownCleaningDomainError)
    assert second_result.code is MarkdownCleaningApiErrorCode.QUEUE_SUBMISSION_FAILED
    assert second_result.http_status == 503
    assert "redis-secret" not in second_result.safe_message
    assert len(dispatcher.task_ids) == 1
