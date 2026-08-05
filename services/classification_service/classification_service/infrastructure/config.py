from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

RuntimeEnvironment = Literal["development", "staging", "production"]
QualityStatus = Literal["experimental", "production-approved"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CLASSIFICATION_", extra="ignore")

    environment: RuntimeEnvironment
    internal_service_token: SecretStr = Field(min_length=1)
    model_root: Path
    model_release: Path
    model_release_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    release_quality_status: QualityStatus
    input_root: Path
    max_input_bytes: int = Field(default=2_000_000, gt=0)
    max_text_chars: int = Field(default=500_000, gt=0)
    inference_workers: Literal[1] = 1
    active_inference_limit: Literal[1] = 1
    waiting_queue_limit: int = Field(default=8, gt=0)
    inference_timeout_seconds: float = Field(default=15.0, gt=0, allow_inf_nan=False)
    minimum_free_gpu_mib: int = Field(default=8192, gt=0)

    @model_validator(mode="after")
    def validate_release_policy(self) -> "Settings":
        if (
            self.environment == "production"
            and self.release_quality_status != "production-approved"
        ):
            raise ValueError("production requires a production-approved model release")

        try:
            self.model_release.resolve().relative_to(self.model_root.resolve())
        except ValueError as error:
            raise ValueError(
                "model_release must be located under model_root"
            ) from error
        if not self.input_root.is_absolute():
            raise ValueError("input_root must be absolute")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings.model_validate({})
