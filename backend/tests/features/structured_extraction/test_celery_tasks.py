import uuid

from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.features.structured_extraction.celery_tasks import handle_submit_task
from app.features.structured_extraction.models import (
    ExtractionTask,
    ExtractionTaskStatus,
)
from app.features.structured_extraction.repository import ExtractionTaskRepository
from app.models import User  # noqa: F401


def make_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def test_minimal_worker_consumes_queued_task_into_safe_terminal_failure() -> None:
    caller_id = uuid.uuid4()
    with make_session() as session:
        task = ExtractionTask(
            caller_id=caller_id,
            session_id="session-1",
            file_id="file-1",
            request_fingerprint="a" * 64,
            file_storage_path="/allowed/input/sample.txt",
            selected_input_type="local",
            target_path="/allowed/output/sample.md",
            status=ExtractionTaskStatus.QUEUED,
        )
        session.add(task)
        session.commit()
        session.refresh(task)
        task_id = task.id

        handle_submit_task(
            session,
            task_id=str(task_id),
            task_type="structured_extraction",
            schema_version=1,
        )

        updated = ExtractionTaskRepository(session).get_for_caller(
            task_id,
            caller_id,
        )
        assert updated is not None
        assert updated.status is ExtractionTaskStatus.FAILED
        assert updated.started_at is not None
        assert updated.finished_at is not None
        assert updated.error_code == "PROCESSING_FAILED"
        assert updated.error_message == "结构化提取处理器尚未启用"


def test_minimal_worker_ignores_duplicate_delivery_after_terminal_state() -> None:
    with make_session() as session:
        task = ExtractionTask(
            caller_id=uuid.uuid4(),
            session_id="session-1",
            file_id="file-1",
            request_fingerprint="a" * 64,
            file_storage_path="/allowed/input/sample.txt",
            selected_input_type="local",
            target_path="/allowed/output/sample.md",
            status=ExtractionTaskStatus.FAILED,
            error_code="PROCESSING_FAILED",
            error_message="结构化提取处理器尚未启用",
        )
        session.add(task)
        session.commit()
        session.refresh(task)

        handle_submit_task(
            session,
            task_id=str(task.id),
            task_type="structured_extraction",
            schema_version=1,
        )

        session.refresh(task)
        assert task.status is ExtractionTaskStatus.FAILED
