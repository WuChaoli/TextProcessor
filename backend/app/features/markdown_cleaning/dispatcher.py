import uuid
from typing import Protocol

from celery import Celery  # type: ignore[import-untyped]

from app.features.markdown_cleaning.messages import MarkdownCleaningMessage


class MarkdownCleaningTaskDispatcher(Protocol):
    def enqueue_execute(self, task_id: uuid.UUID) -> None: ...


class CeleryMarkdownCleaningTaskDispatcher:
    def __init__(self, app: Celery | None = None) -> None:
        if app is None:
            from app.core.celery_app import celery_app

            app = celery_app
        self._app = app

    def enqueue_execute(self, task_id: uuid.UUID) -> None:
        payload = MarkdownCleaningMessage(
            taskId=task_id,
            taskType="markdown_cleaning",
            schemaVersion=1,
        ).as_payload()
        self._app.send_task(
            "markdown_cleaning.execute",
            kwargs=payload,
        )
