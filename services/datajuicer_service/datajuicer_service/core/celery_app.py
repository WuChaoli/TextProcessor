from celery import Celery  # type: ignore[import-untyped]


def create_celery_app(
    *,
    broker_url: str,
    queue: str,
    recovery_interval_seconds: int,
) -> Celery:
    app = Celery(
        "datajuicer_service",
        broker=broker_url,
        set_as_current=False,
    )
    app.conf.update(
        accept_content=["json"],
        task_serializer="json",
        result_backend=None,
        task_ignore_result=True,
        task_default_queue=queue,
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        beat_schedule={
            "datajuicer-recover": {
                "task": "datajuicer.recover",
                "schedule": recovery_interval_seconds,
            }
        },
    )
    return app
