from functools import lru_cache

from pydantic import Field
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
    worker_concurrency: int = Field(default=1, gt=0)
    profile_np: int = Field(default=1, gt=0)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
