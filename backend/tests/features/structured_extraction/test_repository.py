import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.features.structured_extraction.models import (
    ExtractionTask,
    ExtractionTaskStatus,
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
