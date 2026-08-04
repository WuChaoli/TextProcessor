from pathlib import Path

import pytest
from pydantic import SecretStr

from classification_service.infrastructure.config import Settings


def valid_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "development",
        "internal_service_token": SecretStr("secret"),
        "model_root": Path("/models"),
        "model_release": Path("/models/release"),
        "model_release_sha256": "a" * 64,
        "release_quality_status": "production-approved",
    }
    values.update(overrides)
    return Settings(**values)


def test_production_rejects_experimental_release(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="production-approved"):
        Settings(
            environment="production",
            internal_service_token=SecretStr("secret"),
            model_root=tmp_path,
            model_release=tmp_path / "release",
            model_release_sha256="a" * 64,
            release_quality_status="experimental",
        )


def test_inference_capacity_is_fixed() -> None:
    settings = valid_settings()

    assert settings.inference_workers == 1
    assert settings.active_inference_limit == 1
    assert settings.waiting_queue_limit == 8
    assert settings.inference_timeout_seconds == 15
    assert settings.max_text_chars == 500_000
