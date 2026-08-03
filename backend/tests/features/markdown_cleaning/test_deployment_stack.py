import pytest

from app.core.celery_app import celery_app
from app.core.config import settings


@pytest.fixture(scope="session", autouse=True)
def db() -> None:
    """Static deployment assertions do not require PostgreSQL."""


def test_markdown_worker_is_included_and_recovery_is_scheduled_on_its_queue() -> None:
    assert "app.features.markdown_cleaning.celery_tasks" in celery_app.conf.include
    entry = celery_app.conf.beat_schedule["recover-markdown-cleaning-tasks"]
    assert entry["task"] == "markdown_cleaning.recover"
    assert (
        entry["schedule"]
        == settings.MARKDOWN_CLEANING_WORKER.queue_recovery_interval_seconds
    )
    assert entry["options"]["queue"] == "markdown_cleaning"


def test_execute_limits_match_server_configuration() -> None:
    task = celery_app.tasks["markdown_cleaning.execute"]
    configured = settings.MARKDOWN_CLEANING_WORKER
    assert task.soft_time_limit == configured.processing_soft_timeout_seconds
    assert task.time_limit == configured.processing_hard_timeout_seconds
    assert task.time_limit > configured.processing_soft_timeout_seconds + 0.5
