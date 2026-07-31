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
    assert settings.input_max_records == 100_000
    assert settings.input_max_bytes == 1024 * 1024 * 1024
    assert settings.input_max_text_chars == 1_000_000_000
    assert settings.lease_seconds == 300
    assert settings.recovery_batch_size == 100


def test_settings_reject_non_positive_limits() -> None:
    with pytest.raises(ValidationError):
        Settings(
            database_url="postgresql+psycopg://user:pass@localhost/datajuicer_service",
            celery_broker_url="redis://localhost:6379/0",
            max_attempts=0,
        )
