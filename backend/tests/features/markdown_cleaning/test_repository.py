import uuid
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.core.db import engine
from app.features.markdown_cleaning.api_errors import (
    MarkdownCleaningApiErrorCode,
    MarkdownCleaningDomainError,
)
from app.features.markdown_cleaning.repository import (
    ConditionalMarkdownCleaningUpdateFailed,
    MarkdownCleaningTaskRepository,
    request_fingerprint,
)
from app.features.markdown_cleaning.state_machine import (
    MarkdownCleaningTaskStatus,
)
from app.features.markdown_cleaning.task_models import MarkdownCleaningTask
from app.models import User


def _make_task_idempotent(
    caller_id: uuid.UUID,
    *,
    session_id: str,
    file_id: str,
) -> MarkdownCleaningTask:
    fingerprint = request_fingerprint(
        file_storage_path="/data/input.md",
        file_oss_url=None,
        selected_input_type="local",
        target_path="/data/output.md",
    )
    return MarkdownCleaningTask(
        caller_id=caller_id,
        session_id=session_id,
        file_id=file_id,
        request_fingerprint=fingerprint,
        file_storage_path="/data/input.md",
        selected_input_type="local",
        target_path="/data/output.md",
        status=MarkdownCleaningTaskStatus.PENDING,
    )


def _assert_summary_fields_still_null(task: MarkdownCleaningTask) -> None:
    assert task.duplicate_paragraphs_removed is None
    assert task.phone_redaction_count is None
    assert task.id_card_redaction_count is None
    assert task.bank_card_redaction_count is None
    assert task.email_redaction_count is None
    assert task.ipv4_redaction_count is None
    assert task.formatting_change_count is None


@pytest.fixture
def session() -> Generator[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def test_idempotency_key_includes_file_id(session: Session) -> None:
    repository = MarkdownCleaningTaskRepository(session)
    caller_id = uuid.uuid4()
    first, first_created = repository.create_or_get(
        caller_id=caller_id,
        session_id="batch-1",
        file_id="11",
        file_storage_path="C:/input/1.md",
        file_oss_url=None,
        selected_input_type="local",
        target_path="C:/output/1.md",
    )
    second, second_created = repository.create_or_get(
        caller_id=caller_id,
        session_id="batch-1",
        file_id="12",
        file_storage_path="C:/input/2.md",
        file_oss_url=None,
        selected_input_type="local",
        target_path="C:/output/2.md",
    )

    assert first_created
    assert second_created
    assert first.id != second.id


def test_create_or_get_rejects_changed_parameters(session: Session) -> None:
    caller_id = uuid.uuid4()
    repository = MarkdownCleaningTaskRepository(session)
    repository.create_or_get(
        caller_id=caller_id,
        session_id="session-1",
        file_id="11",
        file_storage_path="C:/input/1.md",
        file_oss_url=None,
        selected_input_type="local",
        target_path="C:/output/1.md",
    )

    with pytest.raises(MarkdownCleaningDomainError) as raised:
        repository.create_or_get(
            caller_id=caller_id,
            session_id="session-1",
            file_id="11",
            file_storage_path="C:/input/1.md",
            file_oss_url="https://files.internal/changed.md",
            selected_input_type="remote",
            target_path="C:/output/changed.md",
        )
    assert raised.value.code is MarkdownCleaningApiErrorCode.IDEMPOTENCY_CONFLICT
    assert raised.value.http_status == 409


def test_caller_and_file_session_unique(session: Session) -> None:
    caller_id = uuid.uuid4()
    session.add(
        _make_task_idempotent(
            caller_id,
            session_id="session-1",
            file_id="file-1",
        )
    )
    with pytest.raises(IntegrityError):
        session.add(
            _make_task_idempotent(
                caller_id,
                session_id="session-1",
                file_id="file-1",
            )
        )
        session.commit()


def test_get_for_caller_hides_other_callers(session: Session) -> None:
    repository = MarkdownCleaningTaskRepository(session)
    task, _ = repository.create_or_get(
        caller_id=uuid.uuid4(),
        session_id="session-1",
        file_id="11",
        file_storage_path="C:/input/1.md",
        file_oss_url=None,
        selected_input_type="local",
        target_path="C:/output/1.md",
    )

    assert repository.get_for_caller(task.id, uuid.uuid4()) is None


def test_transition_requires_expected_status(session: Session) -> None:
    repository = MarkdownCleaningTaskRepository(session)
    task, _ = repository.create_or_get(
        caller_id=uuid.uuid4(),
        session_id="session-1",
        file_id="11",
        file_storage_path="C:/input/1.md",
        file_oss_url=None,
        selected_input_type="local",
        target_path="C:/output/1.md",
    )

    with pytest.raises(ConditionalMarkdownCleaningUpdateFailed):
        repository.transition(
            task.id,
            expected=MarkdownCleaningTaskStatus.RUNNING,
            target=MarkdownCleaningTaskStatus.SUCCEEDED,
        )


def test_queue_failure_path_keeps_summary_fields_null_before_completion(
    session: Session,
) -> None:
    repository = MarkdownCleaningTaskRepository(session)
    task, _ = repository.create_or_get(
        caller_id=uuid.uuid4(),
        session_id="session-1",
        file_id="11",
        file_storage_path="C:/input/1.md",
        file_oss_url=None,
        selected_input_type="local",
        target_path="C:/output/1.md",
    )

    task = repository.transition(
        task.id,
        expected=MarkdownCleaningTaskStatus.PENDING,
        target=MarkdownCleaningTaskStatus.QUEUED,
        processing_phase="validating_input",
        queued_at=datetime(2026, 8, 3, tzinfo=UTC),
    )
    task = repository.transition(
        task.id,
        expected=MarkdownCleaningTaskStatus.QUEUED,
        target=MarkdownCleaningTaskStatus.FAILED,
        error_code="QUEUE_SUBMISSION_FAILED",
        error_message="queue submission failed",
        processing_phase="dispatching_failed",
    )

    _assert_summary_fields_still_null(task)
    assert task.status is MarkdownCleaningTaskStatus.FAILED


def test_task_columns_have_database_defaults() -> None:
    task_table = cast(Any, MarkdownCleaningTask).__table__
    processor_contract_version_default = task_table.c[
        "processor_contract_version"
    ].server_default
    max_attempts_default = task_table.c["max_attempts"].server_default

    assert processor_contract_version_default is not None
    assert str(processor_contract_version_default.arg) == "'markdown_cleaning_v1'"
    assert max_attempts_default is not None
    assert str(max_attempts_default.arg) == "3"

    migration_file = Path(
        Path(__file__).resolve().parents[3]
        / "app"
        / "alembic"
        / "versions"
        / "20260803_02_set_markdown_cleaning_task_defaults.py"
    )
    content = migration_file.read_text(encoding="utf-8")
    assert 'server_default="markdown_cleaning_v1"' in content
    assert 'server_default="3"' in content


def test_mark_dispatched_updates_queued_task(session: Session) -> None:
    repository = MarkdownCleaningTaskRepository(session)
    task, _ = repository.create_or_get(
        caller_id=uuid.uuid4(),
        session_id="session-1",
        file_id="11",
        file_storage_path="C:/input/1.md",
        file_oss_url=None,
        selected_input_type="local",
        target_path="C:/output/1.md",
    )
    task = repository.transition(
        task.id,
        expected=MarkdownCleaningTaskStatus.PENDING,
        target=MarkdownCleaningTaskStatus.QUEUED,
    )
    assert repository.mark_dispatched(task.id, now=datetime(2026, 8, 3, tzinfo=UTC))
    updated = repository.get(task.id)
    assert updated is not None
    assert updated.last_dispatched_at is not None


def test_postgresql_concurrent_create_converges_to_one_task(db: Session) -> None:
    if db.get_bind().dialect.name != "postgresql":
        pytest.skip("PostgreSQL is required")
    caller = db.exec(select(User)).first()
    if caller is None:
        pytest.skip("test user missing")

    session_id = f"concurrent-{uuid.uuid7()}"

    def create() -> tuple[uuid.UUID, bool]:
        with Session(engine) as pg_session:
            task, created = MarkdownCleaningTaskRepository(pg_session).create_or_get(
                caller_id=caller.id,
                session_id=session_id,
                file_id="file-1",
                file_storage_path="/data/input.md",
                file_oss_url=None,
                selected_input_type="local",
                target_path="/data/output.md",
            )
            return task.id, created

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: create(), range(2)))

    assert len({task_id for task_id, _created in results}) == 1
    assert sum(created for _task_id, created in results) == 1
