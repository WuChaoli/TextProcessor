import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Protocol

from celery import Task  # type: ignore[import-untyped]
from sqlmodel import Session

from app.core.celery_app import celery_app
from app.core.config import ExtractionWorkerSettings, settings
from app.core.db import engine
from app.features.structured_extraction.errors import (
    ExtractionErrorCode,
    ExtractionProcessingError,
)
from app.features.structured_extraction.orchestration import (
    ExtractionOrchestrator,
    ExtractionTaskScheduler,
    is_retryable_processor_http_error,
)
from app.features.structured_extraction.repository import ExtractionTaskRepository

logger = logging.getLogger(__name__)
_TASK_TYPE = "structured_extraction"
_SCHEMA_VERSION = 1
_TRANSIENT_RETRY_LIMIT = 3


class RecoveryDispatcher(Protocol):
    def enqueue_submit(self, task_id: uuid.UUID) -> None: ...


class CeleryOrchestrationScheduler(ExtractionTaskScheduler):
    def enqueue_submit(self, task_id: uuid.UUID, *, countdown: int) -> None:
        submit_extraction_task.apply_async(
            kwargs=_message(task_id),
            countdown=countdown,
        )

    def enqueue_poll(self, task_id: uuid.UUID, *, countdown: int) -> None:
        poll_extraction_task.apply_async(
            kwargs=_message(task_id),
            countdown=countdown,
        )


def recover_queued_tasks(
    session: Session,
    dispatcher: RecoveryDispatcher,
    *,
    queued_before: datetime,
) -> int:
    """Compatibility helper for the pre-orchestrator lost-submit recovery tests."""
    repository = ExtractionTaskRepository(session)
    recovered = 0
    for task in repository.list_undispatched_queued(queued_before=queued_before):
        try:
            dispatcher.enqueue_submit(task.id)
        except Exception:
            logger.warning(
                "structured extraction recovery dispatch failed",
                extra={
                    "task_id": str(task.id),
                    "error_code": ExtractionErrorCode.QUEUE_SUBMISSION_FAILED,
                },
            )
            continue
        try:
            marked_dispatched = repository.mark_dispatched(task.id)
        except Exception:
            repository.rollback()
            logger.warning(
                "structured extraction recovery marker write failed",
                extra={
                    "task_id": str(task.id),
                    "error_code": ExtractionErrorCode.INTERNAL_ERROR,
                },
            )
            continue
        if marked_dispatched:
            recovered += 1
    return recovered


def handle_submit_task(
    session: Session,
    *,
    task_id: str,
    task_type: str,
    schema_version: int,
    worker_settings: ExtractionWorkerSettings | None = None,
    input_roots: tuple[Path, ...] | None = None,
    max_input_bytes: int | None = None,
    scheduler: ExtractionTaskScheduler | None = None,
) -> None:
    parsed_task_id = _validate_message(task_id, task_type, schema_version)
    _orchestrator(
        session,
        worker_settings=worker_settings,
        input_roots=input_roots,
        max_input_bytes=max_input_bytes,
        scheduler=scheduler,
    ).submit(parsed_task_id)


def handle_poll_task(
    session: Session,
    *,
    task_id: str,
    task_type: str,
    schema_version: int,
    worker_settings: ExtractionWorkerSettings | None = None,
    input_roots: tuple[Path, ...] | None = None,
    max_input_bytes: int | None = None,
    scheduler: ExtractionTaskScheduler | None = None,
) -> None:
    parsed_task_id = _validate_message(task_id, task_type, schema_version)
    _orchestrator(
        session,
        worker_settings=worker_settings,
        input_roots=input_roots,
        max_input_bytes=max_input_bytes,
        scheduler=scheduler,
    ).poll(parsed_task_id)


def handle_recover_task(
    session: Session,
    *,
    now: datetime | None = None,
    worker_settings: ExtractionWorkerSettings | None = None,
    input_roots: tuple[Path, ...] | None = None,
    max_input_bytes: int | None = None,
    scheduler: ExtractionTaskScheduler | None = None,
) -> int:
    return _orchestrator(
        session,
        worker_settings=worker_settings,
        input_roots=input_roots,
        max_input_bytes=max_input_bytes,
        scheduler=scheduler,
    ).recover(now=now)


@celery_app.task(  # type: ignore[untyped-decorator]
    name="structured_extraction.submit",
    bind=True,
    max_retries=_TRANSIENT_RETRY_LIMIT,
)
def submit_extraction_task(
    self: Task,
    task_id: str,
    task_type: str,
    schema_version: int,
) -> None:
    with Session(engine) as session:
        try:
            handle_submit_task(
                session,
                task_id=task_id,
                task_type=task_type,
                schema_version=schema_version,
            )
        except ExtractionProcessingError as error:
            if not is_retryable_processor_http_error(error):
                raise
            raise self.retry(
                exc=error,
                countdown=settings.EXTRACTION_WORKER.poll_interval_seconds,
            ) from error


@celery_app.task(  # type: ignore[untyped-decorator]
    name="structured_extraction.poll",
    bind=True,
    max_retries=_TRANSIENT_RETRY_LIMIT,
)
def poll_extraction_task(
    self: Task,
    task_id: str,
    task_type: str,
    schema_version: int,
) -> None:
    with Session(engine) as session:
        try:
            handle_poll_task(
                session,
                task_id=task_id,
                task_type=task_type,
                schema_version=schema_version,
            )
        except ExtractionProcessingError as error:
            if not is_retryable_processor_http_error(error):
                raise
            raise self.retry(
                exc=error,
                countdown=settings.EXTRACTION_WORKER.poll_interval_seconds,
            ) from error


@celery_app.task(  # type: ignore[untyped-decorator]
    name="structured_extraction.recover"
)
def recover_extraction_tasks() -> int:
    with Session(engine) as session:
        return handle_recover_task(session)


def _orchestrator(
    session: Session,
    *,
    worker_settings: ExtractionWorkerSettings | None,
    input_roots: tuple[Path, ...] | None,
    max_input_bytes: int | None,
    scheduler: ExtractionTaskScheduler | None,
) -> ExtractionOrchestrator:
    return ExtractionOrchestrator(
        session,
        worker_settings=worker_settings or settings.EXTRACTION_WORKER,
        input_roots=input_roots or tuple(settings.EXTRACTION_INPUT_ROOTS),
        max_input_bytes=max_input_bytes or settings.EXTRACTION_MAX_INPUT_BYTES,
        scheduler=scheduler or CeleryOrchestrationScheduler(),
    )


def _message(task_id: uuid.UUID) -> dict[str, object]:
    return {
        "task_id": str(task_id),
        "task_type": _TASK_TYPE,
        "schema_version": _SCHEMA_VERSION,
    }


def _validate_message(
    task_id: str,
    task_type: str,
    schema_version: int,
) -> uuid.UUID:
    if task_type != _TASK_TYPE or schema_version != _SCHEMA_VERSION:
        raise ValueError("不支持的结构化提取任务消息")
    return uuid.UUID(task_id)
