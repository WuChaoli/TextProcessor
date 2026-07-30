import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.features.structured_extraction.errors import (
    ExtractionDomainError,
    ExtractionErrorCode,
)
from app.features.structured_extraction.models import (
    ExtractionTask,
    ExtractionTaskStatus,
)
from app.features.structured_extraction.repository import (
    ConditionalTransitionFailed,
    ExtractionTaskRepository,
)
from app.models import User  # noqa: F401


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


def make_task(
    *,
    caller_id: uuid.UUID | None = None,
    session_id: str = "session-1",
    file_id: str = "file-1",
) -> ExtractionTask:
    return ExtractionTask(
        caller_id=caller_id or uuid.uuid4(),
        session_id=session_id,
        file_id=file_id,
        request_fingerprint="a" * 64,
        file_storage_path="/data/input/1.txt",
        selected_input_type="local",
        target_path="/data/output/1.md",
        status=ExtractionTaskStatus.PENDING,
    )


def test_idempotency_key_is_unique(session: Session) -> None:
    caller_id = uuid.uuid4()
    session.add(make_task(caller_id=caller_id))
    session.commit()
    session.add(make_task(caller_id=caller_id))

    with pytest.raises(IntegrityError):
        session.commit()


def test_different_caller_can_reuse_session_and_file(session: Session) -> None:
    session.add(make_task(caller_id=uuid.uuid4()))
    session.add(make_task(caller_id=uuid.uuid4()))

    session.commit()


def test_create_or_get_returns_existing_for_same_parameters(session: Session) -> None:
    caller_id = uuid.uuid4()
    repository = ExtractionTaskRepository(session)

    first, first_created = repository.create_or_get(
        caller_id=caller_id,
        session_id="session-1",
        file_id="file-1",
        file_storage_path="/data/input/1.txt",
        file_oss_url=None,
        selected_input_type="local",
        target_path="/data/output/1.md",
    )
    second, second_created = repository.create_or_get(
        caller_id=caller_id,
        session_id="session-1",
        file_id="file-1",
        file_storage_path="/data/input/1.txt",
        file_oss_url=None,
        selected_input_type="local",
        target_path="/data/output/1.md",
    )

    assert first_created is True
    assert second_created is False
    assert second.id == first.id


def test_create_or_get_rejects_changed_parameters(session: Session) -> None:
    caller_id = uuid.uuid4()
    repository = ExtractionTaskRepository(session)
    repository.create_or_get(
        caller_id=caller_id,
        session_id="session-1",
        file_id="file-1",
        file_storage_path="/data/input/1.txt",
        file_oss_url=None,
        selected_input_type="local",
        target_path="/data/output/1.md",
    )

    with pytest.raises(ExtractionDomainError) as raised:
        repository.create_or_get(
            caller_id=caller_id,
            session_id="session-1",
            file_id="file-1",
            file_storage_path="/data/input/1.txt",
            file_oss_url=None,
            selected_input_type="local",
            target_path="/data/output/changed.md",
        )

    assert raised.value.code is ExtractionErrorCode.IDEMPOTENCY_CONFLICT
    assert raised.value.http_status == 409


def test_get_for_caller_hides_other_callers(session: Session) -> None:
    repository = ExtractionTaskRepository(session)
    task, _ = repository.create_or_get(
        caller_id=uuid.uuid4(),
        session_id="session-1",
        file_id="file-1",
        file_storage_path="/data/input/1.txt",
        file_oss_url=None,
        selected_input_type="local",
        target_path="/data/output/1.md",
    )

    assert repository.get_for_caller(task.id, uuid.uuid4()) is None


def test_transition_requires_expected_current_status(session: Session) -> None:
    repository = ExtractionTaskRepository(session)
    task, _ = repository.create_or_get(
        caller_id=uuid.uuid4(),
        session_id="session-1",
        file_id="file-1",
        file_storage_path="/data/input/1.txt",
        file_oss_url=None,
        selected_input_type="local",
        target_path="/data/output/1.md",
    )

    with pytest.raises(ConditionalTransitionFailed):
        repository.transition(
            task.id,
            expected=ExtractionTaskStatus.RUNNING,
            target=ExtractionTaskStatus.SUCCEEDED,
        )
