import uuid

from celery import Celery  # type: ignore[import-untyped]

from app.core.celery_app import celery_app


class CeleryExtractionTaskDispatcher:
    def __init__(self, app: Celery = celery_app) -> None:
        self._app = app

    def enqueue_submit(self, task_id: uuid.UUID) -> None:
        self._app.send_task(
            "structured_extraction.submit",
            kwargs={
                "task_id": str(task_id),
                "task_type": "structured_extraction",
                "schema_version": 1,
            },
        )
