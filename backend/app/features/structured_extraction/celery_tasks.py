import logging
import uuid

from sqlmodel import Session

from app.core.celery_app import celery_app
from app.core.db import engine
from app.features.structured_extraction.errors import ExtractionErrorCode
from app.features.structured_extraction.models import (
    ExtractionTask,
    ExtractionTaskStatus,
    get_datetime_utc,
)
from app.features.structured_extraction.repository import ExtractionTaskRepository

logger = logging.getLogger(__name__)


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
    running = repository.transition(
        task.id,
        expected=ExtractionTaskStatus.QUEUED,
        target=ExtractionTaskStatus.RUNNING,
        started_at=get_datetime_utc(),
        attempt_count=task.attempt_count + 1,
    )
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
