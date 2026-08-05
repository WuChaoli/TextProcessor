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
        "input_root": Path("C:/classification-staging"),
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
            input_root=tmp_path,
        )


def test_inference_capacity_is_fixed() -> None:
    settings = valid_settings()

    assert settings.inference_workers == 1
    assert settings.active_inference_limit == 1
    assert settings.waiting_queue_limit == 8
    assert settings.inference_timeout_seconds == 15
    assert settings.max_text_chars == 500_000
    assert settings.max_input_bytes == 2_000_000


def test_rejects_empty_internal_service_token() -> None:
    with pytest.raises(ValueError):
        valid_settings(internal_service_token=SecretStr(""))


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("max_text_chars", 0),
        ("max_input_bytes", 0),
        ("waiting_queue_limit", -1),
        ("inference_timeout_seconds", float("nan")),
        ("minimum_free_gpu_mib", 0),
    ],
)
def test_rejects_non_positive_or_non_finite_capacity_limits(
    field_name: str, invalid_value: object
) -> None:
    with pytest.raises(ValueError):
        valid_settings(**{field_name: invalid_value})


def test_rejects_model_release_outside_model_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="under model_root"):
        valid_settings(model_root=tmp_path, model_release=tmp_path.parent / "release")


def test_rejects_model_release_path_traversal_outside_model_root(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="under model_root"):
        valid_settings(model_root=tmp_path, model_release=tmp_path / ".." / "release")


def test_accepts_model_release_under_model_root(tmp_path: Path) -> None:
    settings = valid_settings(model_root=tmp_path, model_release=tmp_path / "release")

    assert settings.model_release == tmp_path / "release"
