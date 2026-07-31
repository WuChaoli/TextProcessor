import pytest
from pydantic import ValidationError

from datajuicer_service.core.config import Settings


def test_settings_use_isolated_queue_defaults() -> None:
    settings = Settings(
        database_url="postgresql+psycopg://user:pass@localhost/datajuicer_service",
        celery_broker_url="redis://localhost:6379/0",
    )

    assert settings.celery_queue == "datajuicer.jobs"
    assert settings.max_attempts == 3
    assert settings.worker_concurrency == 1


def test_settings_reject_non_positive_limits() -> None:
    with pytest.raises(ValidationError):
        Settings(
            database_url="postgresql+psycopg://user:pass@localhost/datajuicer_service",
            celery_broker_url="redis://localhost:6379/0",
            max_attempts=0,
        )
