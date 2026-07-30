from celery import Celery  # type: ignore[import-untyped]

from app.core.config import settings

celery_app = Celery(
    "text_processor",
    broker=settings.CELERY_BROKER_URL,
)
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_backend=None,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
)
