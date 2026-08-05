import uuid
from datetime import datetime
from typing import Protocol

import httpx
from celery import Task  # type: ignore[import-untyped]
from sqlmodel import Session

from app.core.celery_app import celery_app
from app.core.config import settings
from app.core.db import engine
from app.features.text_classification.adapter import ClassificationClient
from app.features.text_classification.input_preparer import ClassificationInputPreparer
from app.features.text_classification.models import utc_now
from app.features.text_classification.repository import ClassificationTaskRepository
from app.tasking.envelope import TaskEnvelope
from app.tasking.state import TaskStatus

_LEASE_SECONDS = 300


class ClassificationDispatcher(Protocol):
    def enqueue(self, task_id: uuid.UUID) -> None: ...


class RetryableClassificationError(RuntimeError):
    pass


def execute(session: Session, task_id: uuid.UUID) -> None:
    repository = ClassificationTaskRepository(session)
    task = repository.get(task_id)
    if task is None or task.status in {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
        return
    task = repository.claim_for_execution(
        task_id,
        now=utc_now(),
        lease_seconds=_LEASE_SECONDS,
    )
    if task is None:
        return
    try:
        prepared = ClassificationInputPreparer(staging_root=settings.CLASSIFICATION_STAGING_ROOT, input_roots=tuple(settings.CLASSIFICATION_INPUT_ROOTS), max_input_bytes=settings.CLASSIFICATION_MAX_INPUT_BYTES).prepare(str(task.id), task.input_uri)
        result = ClassificationClient(
            base_url=settings.CLASSIFICATION_BASE_URL,
            api_token=settings.CLASSIFICATION_API_TOKEN,
            timeout=settings.CLASSIFICATION_TIMEOUT_SECONDS,
        ).classify(str(task.id), prepared.local_uri)
        repository.transition(task.id, expected=TaskStatus.RUNNING, target=TaskStatus.SUCCEEDED, staging_uri=prepared.local_uri, input_sha256=prepared.input_sha256, input_size_bytes=prepared.size_bytes, result=result, lease_expires_at=None, finished_at=utc_now())
    except Exception as error:
        if _is_retryable(error) and task.attempt_count < task.max_attempts:
            repository.update_running(task.id, lease_expires_at=utc_now())
            raise RetryableClassificationError from error
        repository.transition(task.id, expected=TaskStatus.RUNNING, target=TaskStatus.FAILED, error_code="CLASSIFICATION_FAILED", error_message="分类处理失败", lease_expires_at=None, finished_at=utc_now())


def _is_retryable(error: Exception) -> bool:
    if isinstance(error, (httpx.ConnectError, httpx.TimeoutException)):
        return True
    return isinstance(error, httpx.HTTPStatusError) and error.response.status_code in {
        429,
        502,
        503,
        504,
    }


def recover(
    session: Session,
    dispatcher: ClassificationDispatcher,
    *,
    now: datetime,
    limit: int,
) -> int:
    repository = ClassificationTaskRepository(session)
    recovered = 0
    for task in repository.list_recoverable(now=now, limit=limit):
        if task.status is TaskStatus.RUNNING and task.attempt_count >= task.max_attempts:
            repository.transition(
                task.id,
                expected=TaskStatus.RUNNING,
                target=TaskStatus.FAILED,
                error_code="ATTEMPTS_EXHAUSTED",
                error_message="分类任务重试次数已耗尽",
                lease_expires_at=None,
                finished_at=now,
            )
            continue
        try:
            dispatcher.enqueue(task.id)
        except Exception:
            continue
        repository.mark_dispatched(task.id)
        recovered += 1
    return recovered


@celery_app.task(bind=True, name="text_classification.execute", max_retries=2)  # type: ignore[untyped-decorator]
def execute_task(self: Task, *, task_id: str, task_type: str, schema_version: int) -> None:
    envelope = TaskEnvelope.parse({"task_id": task_id, "task_type": task_type, "schema_version": schema_version}, expected_type="text_classification", expected_schema_version=1)
    with Session(engine) as session:
        try:
            execute(session, envelope.task_id)
        except RetryableClassificationError as error:
            raise self.retry(exc=error, countdown=min(2 ** self.request.retries, 30)) from error


@celery_app.task(name="text_classification.recover")  # type: ignore[untyped-decorator]
def recover_tasks() -> int:
    from app.features.text_classification.dispatcher import (
        CeleryClassificationDispatcher,
    )

    with Session(engine) as session:
        return recover(
            session,
            CeleryClassificationDispatcher(),
            now=utc_now(),
            limit=settings.CLASSIFICATION_RECOVERY_BATCH_SIZE,
        )
