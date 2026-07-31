import uuid
from datetime import timedelta

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.core.config import ExtractionWorkerSettings
from app.features.structured_extraction.celery_tasks import (
    handle_submit_task,
    recover_queued_tasks,
)
from app.features.structured_extraction.models import (
    ExtractionTask,
    ExtractionTaskStatus,
    get_datetime_utc,
)
from app.features.structured_extraction.repository import (
    ConditionalTransitionFailed,
    ExtractionTaskRepository,
)
from app.models import User  # noqa: F401


def make_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def test_minimal_worker_processes_plain_text_to_markdown(tmp_path) -> None:
    caller_id = uuid.uuid4()
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    staging_root = tmp_path / "staging"
    input_root.mkdir()
    output_root.mkdir()
    source = input_root / "sample.txt"
    source.write_text("标题\n\n正文保持不变。\n", encoding="utf-8")
    target = output_root / "sample.md"
    with make_session() as session:
        task = ExtractionTask(
            caller_id=caller_id,
            session_id="session-1",
            file_id="file-1",
            request_fingerprint="a" * 64,
            file_storage_path=str(source),
            selected_input_type="local",
            target_path=str(target),
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
            worker_settings=ExtractionWorkerSettings(
                staging_root=staging_root,
                output_roots=(output_root,),
                production_formats=("text",),
            ),
            input_roots=(input_root,),
            max_input_bytes=1024,
        )

        updated = ExtractionTaskRepository(session).get_for_caller(
            task_id,
            caller_id,
        )
        assert updated is not None
        assert updated.status is ExtractionTaskStatus.SUCCEEDED
        assert updated.started_at is not None
        assert updated.finished_at is not None
        assert updated.processor_name == "plain_text"
        assert updated.detected_format == "text"
        assert updated.input_sha256 is not None
        assert updated.output_sha256 is not None
        assert target.read_text(encoding="utf-8") == "标题\n\n正文保持不变。\n"


def test_worker_rejects_existing_target_before_staging(tmp_path) -> None:
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    staging_root = tmp_path / "staging"
    input_root.mkdir()
    output_root.mkdir()
    source = input_root / "sample.txt"
    source.write_text("content", encoding="utf-8")
    target = output_root / "sample.md"
    target.write_text("existing", encoding="utf-8")
    with make_session() as session:
        task = ExtractionTask(
            caller_id=uuid.uuid4(),
            session_id="session-conflict",
            file_id="file-1",
            request_fingerprint="f" * 64,
            file_storage_path=str(source),
            selected_input_type="local",
            target_path=str(target),
            status=ExtractionTaskStatus.QUEUED,
        )
        session.add(task)
        session.commit()
        session.refresh(task)

        handle_submit_task(
            session,
            task_id=str(task.id),
            task_type="structured_extraction",
            schema_version=1,
            worker_settings=ExtractionWorkerSettings(
                staging_root=staging_root,
                output_roots=(output_root,),
                production_formats=("text",),
            ),
            input_roots=(input_root,),
            max_input_bytes=1024,
        )

        session.refresh(task)
        assert task.status is ExtractionTaskStatus.FAILED
        assert task.error_code == "OUTPUT_CONFLICT"
        assert not staging_root.exists()


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


class RecordingDispatcher:
    def __init__(self) -> None:
        self.task_ids: list[uuid.UUID] = []

    def enqueue_submit(self, task_id: uuid.UUID) -> None:
        self.task_ids.append(task_id)


def test_recovery_redispatches_old_queued_task_without_dispatch_timestamp() -> None:
    dispatcher = RecordingDispatcher()
    with make_session() as session:
        task = ExtractionTask(
            caller_id=uuid.uuid4(),
            session_id="session-recovery",
            file_id="file-1",
            request_fingerprint="c" * 64,
            file_storage_path="/allowed/input/sample.txt",
            selected_input_type="local",
            target_path="/allowed/output/sample.md",
            status=ExtractionTaskStatus.QUEUED,
            queued_at=get_datetime_utc() - timedelta(minutes=5),
        )
        session.add(task)
        session.commit()
        session.refresh(task)

        recovered = recover_queued_tasks(
            session,
            dispatcher,
            queued_before=get_datetime_utc() - timedelta(minutes=1),
        )

        session.refresh(task)
        assert recovered == 1
        assert dispatcher.task_ids == [task.id]
        assert task.last_dispatched_at is not None


def test_recovery_ignores_task_already_marked_dispatched() -> None:
    dispatcher = RecordingDispatcher()
    with make_session() as session:
        task = ExtractionTask(
            caller_id=uuid.uuid4(),
            session_id="session-recovery",
            file_id="file-1",
            request_fingerprint="d" * 64,
            file_storage_path="/allowed/input/sample.txt",
            selected_input_type="local",
            target_path="/allowed/output/sample.md",
            status=ExtractionTaskStatus.QUEUED,
            queued_at=get_datetime_utc() - timedelta(minutes=5),
            last_dispatched_at=get_datetime_utc() - timedelta(minutes=4),
        )
        session.add(task)
        session.commit()

        recovered = recover_queued_tasks(
            session,
            dispatcher,
            queued_before=get_datetime_utc() - timedelta(minutes=1),
        )

        assert recovered == 0
        assert dispatcher.task_ids == []


def test_duplicate_worker_claim_is_treated_as_benign(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with make_session() as session:
        task = ExtractionTask(
            caller_id=uuid.uuid4(),
            session_id="session-duplicate",
            file_id="file-1",
            request_fingerprint="e" * 64,
            file_storage_path="/allowed/input/sample.txt",
            selected_input_type="local",
            target_path="/allowed/output/sample.md",
            status=ExtractionTaskStatus.QUEUED,
        )
        session.add(task)
        session.commit()
        session.refresh(task)

        def lose_claim(*_args: object, **_kwargs: object) -> None:
            raise ConditionalTransitionFailed("another worker claimed task")

        monkeypatch.setattr(
            ExtractionTaskRepository,
            "transition",
            lose_claim,
        )

        handle_submit_task(
            session,
            task_id=str(task.id),
            task_type="structured_extraction",
            schema_version=1,
        )

        session.refresh(task)
        assert task.status is ExtractionTaskStatus.QUEUED


def test_recovery_marker_failure_does_not_abort_remaining_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatcher = RecordingDispatcher()
    with make_session() as session:
        tasks = [
            ExtractionTask(
                caller_id=uuid.uuid4(),
                session_id=f"session-recovery-{index}",
                file_id="file-1",
                request_fingerprint=str(index) * 64,
                file_storage_path="/allowed/input/sample.txt",
                selected_input_type="local",
                target_path=f"/allowed/output/sample-{index}.md",
                status=ExtractionTaskStatus.QUEUED,
                queued_at=get_datetime_utc() - timedelta(minutes=5),
            )
            for index in (1, 2)
        ]
        session.add_all(tasks)
        session.commit()

        def fail_marker(*_args: object, **_kwargs: object) -> bool:
            raise RuntimeError("marker write failed")

        monkeypatch.setattr(
            ExtractionTaskRepository,
            "mark_dispatched",
            fail_marker,
        )

        recovered = recover_queued_tasks(
            session,
            dispatcher,
            queued_before=get_datetime_utc() - timedelta(minutes=1),
        )

        assert recovered == 0
        assert set(dispatcher.task_ids) == {task.id for task in tasks}
