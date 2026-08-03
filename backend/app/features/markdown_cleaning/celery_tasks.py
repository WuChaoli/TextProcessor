from __future__ import annotations

from typing import Any

from celery import Task  # type: ignore[import-untyped]

from app.core.celery_app import celery_app
from app.core.config import settings
from app.features.markdown_cleaning.dependencies import (
    build_orchestrator,
    build_recovery,
    session_scope,
)
from app.features.markdown_cleaning.messages import MarkdownCleaningMessage
from app.features.markdown_cleaning.orchestration import (
    MarkdownCleaningOrchestrator,
    MarkdownCleaningRecovery,
    RetryableWorkerError,
)

_WORKER = settings.MARKDOWN_CLEANING_WORKER
_RETRY_LIMIT = max(0, _WORKER.max_attempts - 1)


def handle_execute_task(
    payload: object, *, orchestrator: MarkdownCleaningOrchestrator
) -> None:
    message = MarkdownCleaningMessage.parse(payload)
    orchestrator.execute(message.task_id)


def handle_recover_task(*, recovery: MarkdownCleaningRecovery) -> dict[str, int]:
    result = recovery.recover_batch()
    return {
        "queuedErrors": result.queued_errors,
        "runningErrors": result.running_errors,
        "preparedErrors": result.prepared_errors,
    }


def _run_execute(payload: object) -> None:
    with session_scope() as session:
        handle_execute_task(payload, orchestrator=build_orchestrator(session))


@celery_app.task(  # type: ignore[untyped-decorator]
    name="markdown_cleaning.execute",
    bind=True,
    max_retries=_RETRY_LIMIT,
    acks_late=True,
    reject_on_worker_lost=True,
    soft_time_limit=_WORKER.processing_soft_timeout_seconds,
    time_limit=_WORKER.processing_hard_timeout_seconds,
    queue="markdown_cleaning",
)
def execute_markdown_cleaning_task(self: Task, **payload: Any) -> None:
    try:
        _run_execute(payload)
    except RetryableWorkerError as exc:
        raise self.retry(
            exc=exc, countdown=_WORKER.queue_recovery_interval_seconds
        ) from exc


@celery_app.task(  # type: ignore[untyped-decorator]
    name="markdown_cleaning.recover",
    acks_late=True,
    reject_on_worker_lost=True,
    queue="markdown_cleaning",
)
def recover_markdown_cleaning_tasks() -> dict[str, int]:
    with session_scope() as session:
        return handle_recover_task(recovery=build_recovery(session))
