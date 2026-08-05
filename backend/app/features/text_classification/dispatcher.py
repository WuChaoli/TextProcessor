import uuid

from celery import Celery  # type: ignore[import-untyped]

from app.core.celery_app import celery_app
from app.tasking.envelope import TaskEnvelope


class CeleryClassificationDispatcher:
    def __init__(self, app: Celery = celery_app) -> None:
        self._app = app

    def enqueue(self, task_id: uuid.UUID) -> None:
        envelope = TaskEnvelope(task_id, "text_classification", 1)
        self._app.send_task("text_classification.execute", kwargs=envelope.as_payload(), queue="text_classification")
