import uuid
from contextlib import contextmanager
from unittest.mock import Mock

import pytest

from app.core.celery_app import celery_app
from app.core.config import MarkdownCleaningWorkerSettings
from app.features.markdown_cleaning import dependencies
from app.features.markdown_cleaning.celery_tasks import (
    execute_markdown_cleaning_task,
    handle_execute_task,
    handle_recover_task,
)
from app.features.markdown_cleaning.messages import InvalidMarkdownCleaningMessage
from app.features.markdown_cleaning.orchestration import RetryableWorkerError


@pytest.fixture(scope="session", autouse=True)
def db() -> None:
    """Task 5 unit tests use fakes; real PostgreSQL belongs to Task 6."""


def message(task_id: uuid.UUID) -> dict[str, object]:
    return {"taskId": str(task_id), "taskType": "markdown_cleaning", "schemaVersion": 1}


def test_execute_handler_accepts_only_strict_envelope() -> None:
    task_id = uuid.uuid7()
    orchestrator = Mock()
    handle_execute_task(message(task_id), orchestrator=orchestrator)
    orchestrator.execute.assert_called_once_with(task_id)
    with pytest.raises(InvalidMarkdownCleaningMessage):
        handle_execute_task(
            {**message(task_id), "sourcePath": "secret"}, orchestrator=orchestrator
        )


def test_recover_handler_returns_isolated_error_counts() -> None:
    recovery = Mock()
    recovery.recover_batch.return_value.queued_errors = 1
    recovery.recover_batch.return_value.running_errors = 2
    recovery.recover_batch.return_value.prepared_errors = 3
    assert handle_recover_task(recovery=recovery) == {
        "queuedErrors": 1,
        "runningErrors": 2,
        "preparedErrors": 3,
    }


def test_tasks_are_registered_with_worker_safety_options() -> None:
    assert {"markdown_cleaning.execute", "markdown_cleaning.recover"} <= set(
        celery_app.tasks
    )
    task = celery_app.tasks["markdown_cleaning.execute"]
    assert task.acks_late is True
    assert task.reject_on_worker_lost is True
    assert task.soft_time_limit is not None and task.time_limit is not None
    assert task.time_limit > task.soft_time_limit
    assert task.max_retries == 2


def test_execute_uses_new_session_scope_for_each_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions: list[object] = []
    orchestrator = Mock()

    @contextmanager
    def scope():
        session = object()
        sessions.append(session)
        yield session

    monkeypatch.setattr(
        "app.features.markdown_cleaning.celery_tasks.session_scope", scope
    )
    monkeypatch.setattr(
        "app.features.markdown_cleaning.celery_tasks.build_orchestrator",
        lambda session: orchestrator,
    )
    task = execute_markdown_cleaning_task._get_current_object()
    task.run(**message(uuid.uuid7()))
    task.run(**message(uuid.uuid7()))
    assert len(sessions) == 2 and sessions[0] is not sessions[1]


def test_only_retryable_worker_error_uses_bounded_celery_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = execute_markdown_cleaning_task._get_current_object()
    monkeypatch.setattr(
        "app.features.markdown_cleaning.celery_tasks._run_execute",
        Mock(side_effect=RetryableWorkerError("later")),
    )
    retry = Mock(side_effect=RuntimeError("retried"))
    monkeypatch.setattr(task, "retry", retry)
    with pytest.raises(RuntimeError, match="retried"):
        task.run(**message(uuid.uuid7()))
    retry.assert_called_once()


def test_non_retryable_error_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    task = execute_markdown_cleaning_task._get_current_object()
    monkeypatch.setattr(
        "app.features.markdown_cleaning.celery_tasks._run_execute",
        Mock(side_effect=ValueError("bad")),
    )
    retry = Mock()
    monkeypatch.setattr(task, "retry", retry)
    with pytest.raises(ValueError, match="bad"):
        task.run(**message(uuid.uuid7()))
    retry.assert_not_called()


def test_factory_injects_processing_timeout_and_heartbeat_uses_fresh_session(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = MarkdownCleaningWorkerSettings(
        staging_root=tmp_path / "staging",
        output_roots=(tmp_path / "output",),
        processing_soft_timeout_seconds=17,
        processing_hard_timeout_seconds=30,
    )
    orchestrator = dependencies.build_orchestrator(Mock(), configured=configured)
    assert orchestrator._processing_timeout_seconds == 17
    assert orchestrator._processor._limits.processing_timeout_seconds == 17

    heartbeat_sessions: list[object] = []
    repository = Mock()
    repository.renew_lease.return_value = True

    @contextmanager
    def heartbeat_scope():
        session = object()
        heartbeat_sessions.append(session)
        yield session

    monkeypatch.setattr(dependencies, "session_scope", heartbeat_scope)
    monkeypatch.setattr(
        dependencies, "MarkdownCleaningTaskRepository", lambda session: repository
    )
    task_id = uuid.uuid7()
    assert dependencies.renew_lease(task_id, "lease", configured=configured)
    assert dependencies.renew_lease(task_id, "lease", configured=configured)
    assert heartbeat_sessions[0] is not heartbeat_sessions[1]
    assert repository.renew_lease.call_args.args == (task_id,)
    assert repository.renew_lease.call_args.kwargs["lease_token"] == "lease"
    assert (
        repository.renew_lease.call_args.kwargs["lease_seconds"]
        == configured.queue_lease_seconds
    )
