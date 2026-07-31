from datajuicer_service.core.celery_app import create_celery_app
from datajuicer_service.core.config import Settings
from datajuicer_service.worker import create_worker_application


def test_celery_uses_isolated_reliable_worker_configuration() -> None:
    app = create_celery_app(
        broker_url="redis://localhost:6379/7",
        queue="datajuicer.jobs",
        recovery_interval_seconds=30,
    )

    assert app.conf.task_default_queue == "datajuicer.jobs"
    assert app.conf.task_serializer == "json"
    assert app.conf.accept_content == ["json"]
    assert app.conf.result_backend is None
    assert app.conf.task_acks_late is True
    assert app.conf.task_reject_on_worker_lost is True
    assert app.conf.worker_prefetch_multiplier == 1
    assert app.conf.beat_schedule["datajuicer-recover"]["task"] == "datajuicer.recover"


def test_worker_application_registers_execute_and_recovery_tasks() -> None:
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        celery_broker_url="memory://",
    )

    app = create_worker_application(settings)

    assert "datajuicer.execute" in app.tasks
    assert "datajuicer.recover" in app.tasks
    assert app.conf.worker_concurrency == 1
