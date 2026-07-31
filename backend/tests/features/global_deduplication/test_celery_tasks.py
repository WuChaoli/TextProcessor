import uuid
from unittest.mock import Mock

import pytest

from app.core.celery_app import celery_app
from app.features.global_deduplication.celery_tasks import (
    CeleryGlobalDeduplicationScheduler,
    handle_poll_task,
    handle_recover_task,
    handle_submit_task,
)
from app.features.global_deduplication.messages import (
    InvalidGlobalDeduplicationMessage,
)


def message(task_id: uuid.UUID) -> dict[str, object]:
    return {
        "taskId": str(task_id),
        "taskType": "global_deduplication",
        "schemaVersion": 1,
    }


@pytest.mark.parametrize(
    "handler,method",
    [
        (handle_submit_task, "submit"),
        (handle_poll_task, "poll"),
    ],
)
def test_handlers_accept_only_minimal_message(
    handler: object,
    method: str,
) -> None:
    task_id = uuid.uuid7()
    orchestrator = Mock()

    handler(message(task_id), orchestrator=orchestrator)  # type: ignore[operator]

    getattr(orchestrator, method).assert_called_once_with(task_id)
    with pytest.raises(InvalidGlobalDeduplicationMessage):
        handler(  # type: ignore[operator]
            {**message(task_id), "fileStoragePath": "/secret.txt"},
            orchestrator=orchestrator,
        )


def test_recover_handler_calls_dispatch_only_scan() -> None:
    orchestrator = Mock()
    orchestrator.recover.return_value.submit_dispatched = 2
    orchestrator.recover.return_value.poll_dispatched = 3

    result = handle_recover_task(orchestrator=orchestrator)

    assert result == {"submitDispatched": 2, "pollDispatched": 3}
    orchestrator.recover.assert_called_once_with()


def test_global_deduplication_tasks_are_registered() -> None:
    assert {
        "global_deduplication.submit",
        "global_deduplication.poll",
        "global_deduplication.recover",
    } <= set(celery_app.tasks)


def test_scheduler_emits_only_schema_message(monkeypatch: pytest.MonkeyPatch) -> None:
    task_id = uuid.uuid7()
    submit = Mock()
    poll = Mock()
    monkeypatch.setattr(
        "app.features.global_deduplication.celery_tasks."
        "submit_global_deduplication_task.apply_async",
        submit,
    )
    monkeypatch.setattr(
        "app.features.global_deduplication.celery_tasks."
        "poll_global_deduplication_task.apply_async",
        poll,
    )
    scheduler = CeleryGlobalDeduplicationScheduler()

    scheduler.enqueue_submit(task_id, countdown=1)
    scheduler.enqueue_poll(task_id, countdown=2)

    submit.assert_called_once_with(kwargs=message(task_id), countdown=1)
    poll.assert_called_once_with(kwargs=message(task_id), countdown=2)
