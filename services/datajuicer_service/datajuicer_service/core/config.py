from functools import lru_cache
from typing import Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DATAJUICER_",
        env_file=".env",
        extra="ignore",
    )

    database_url: str
    celery_broker_url: str
    celery_queue: str = "datajuicer.jobs"
    job_timeout_seconds: int = Field(default=3600, gt=0)
    max_attempts: int = Field(default=3, gt=0)
    recovery_interval_seconds: int = Field(default=30, gt=0)
    recovery_batch_size: int = Field(default=100, gt=0)
    worker_concurrency: int = Field(default=1, gt=0)
    lease_seconds: int = Field(default=300, gt=0)
    lease_heartbeat_seconds: float = Field(default=30, gt=0)
    profile_np: int = Field(default=1, gt=0)
    input_max_records: int = Field(default=100_000, gt=0)
    input_max_bytes: int = Field(default=1024 * 1024 * 1024, gt=0)
    input_max_text_chars: int = Field(default=1_000_000_000, gt=0)

    @model_validator(mode="after")
    def validate_heartbeat(self) -> Self:
        if self.lease_heartbeat_seconds >= self.lease_seconds:
            raise ValueError("lease heartbeat must be shorter than lease")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
