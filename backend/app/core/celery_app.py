from celery import Celery  # type: ignore[import-untyped]

from app.core.config import settings

celery_app = Celery(
    "text_processor",
    broker=settings.CELERY_BROKER_URL,
    include=[
        "app.features.structured_extraction.celery_tasks",
        "app.features.global_deduplication.celery_tasks",
    ],
)
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_backend=None,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    broker_transport_options={
        "visibility_timeout": settings.CELERY_BROKER_VISIBILITY_TIMEOUT_SECONDS,
    },
    beat_schedule={
        "recover-structured-extraction-tasks": {
            "task": "structured_extraction.recover",
            "schedule": settings.EXTRACTION_QUEUE_RECOVERY_INTERVAL_SECONDS,
        },
        "recover-global-deduplication-tasks": {
            "task": "global_deduplication.recover",
            "schedule": settings.GLOBAL_DEDUP_WORKER.recovery_interval_seconds,
        }
    },
)
