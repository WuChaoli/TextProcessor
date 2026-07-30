import logging
import uuid
from datetime import datetime, timedelta
from typing import Protocol

from sqlmodel import Session

from app.core.celery_app import celery_app
from app.core.config import settings
from app.core.db import engine
from app.features.structured_extraction.dispatcher import (
    CeleryExtractionTaskDispatcher,
)
from app.features.structured_extraction.errors import ExtractionErrorCode
from app.features.structured_extraction.models import (
    ExtractionTask,
    ExtractionTaskStatus,
    get_datetime_utc,
)
from app.features.structured_extraction.repository import (
    ConditionalTransitionFailed,
    ExtractionTaskRepository,
)

logger = logging.getLogger(__name__)


class RecoveryDispatcher(Protocol):
    def enqueue_submit(self, task_id: uuid.UUID) -> None: ...


def recover_queued_tasks(
    session: Session,
    dispatcher: RecoveryDispatcher,
    *,
    queued_before: datetime,
) -> int:
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
) -> None:
    if task_type != "structured_extraction" or schema_version != 1:
        raise ValueError("不支持的结构化提取任务消息")
    parsed_task_id = uuid.UUID(task_id)
    task = session.get(ExtractionTask, parsed_task_id)
    if task is None:
        logger.warning(
            "structured extraction task not found",
            extra={
                "task_id": task_id,
                "error_code": ExtractionErrorCode.TASK_NOT_FOUND,
            },
        )
        return
    if task.status is not ExtractionTaskStatus.QUEUED:
        return

    repository = ExtractionTaskRepository(session)
    try:
        running = repository.transition(
            task.id,
            expected=ExtractionTaskStatus.QUEUED,
            target=ExtractionTaskStatus.RUNNING,
            started_at=get_datetime_utc(),
            attempt_count=task.attempt_count + 1,
        )
    except ConditionalTransitionFailed:
        return
    repository.transition(
        running.id,
        expected=ExtractionTaskStatus.RUNNING,
        target=ExtractionTaskStatus.FAILED,
        error_code=ExtractionErrorCode.PROCESSING_FAILED,
        error_message="结构化提取处理器尚未启用",
        finished_at=get_datetime_utc(),
    )


@celery_app.task(  # type: ignore[untyped-decorator]
    name="structured_extraction.submit"
)
def submit_extraction_task(
    task_id: str,
    task_type: str,
    schema_version: int,
) -> None:
    with Session(engine) as session:
        handle_submit_task(
            session,
            task_id=task_id,
            task_type=task_type,
            schema_version=schema_version,
        )


@celery_app.task(  # type: ignore[untyped-decorator]
    name="structured_extraction.recover_queued"
)
def recover_queued_extraction_tasks() -> int:
    queued_before = get_datetime_utc() - timedelta(
        seconds=settings.EXTRACTION_QUEUE_RECOVERY_AFTER_SECONDS
    )
    with Session(engine) as session:
        return recover_queued_tasks(
            session,
            CeleryExtractionTaskDispatcher(),
            queued_before=queued_before,
        )
