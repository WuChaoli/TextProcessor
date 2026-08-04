from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_dockerfile_contract() -> None:
    content = (ROOT / "services/classification_service/Dockerfile").read_text(
        encoding="utf-8"
    )
    assert "nvidia/cuda:13.0.0-cudnn-runtime-ubuntu24.04" in content
    assert "uv sync --locked --no-dev" in content
    assert "HF_HUB_OFFLINE=1" in content
    assert "TRANSFORMERS_OFFLINE=1" in content
    assert '"--workers", "1"' in content


def test_compose_service_is_internal_gpu_only() -> None:
    content = (ROOT / "compose.yml").read_text(encoding="utf-8")
    section = content.split("  classification-service:", 1)[1].split("\n  redis:", 1)[0]
    assert "ports:" not in section
    assert "traefik" not in section.lower()
    assert "depends_on:" not in section
    assert ":/models/releases:ro" in section
    assert "count: 1" in section
    assert "/health/ready" in section
    assert "CLASSIFICATION_INTERNAL_SERVICE_TOKEN" in section
    assert "HF_HUB_OFFLINE=1" in section
    assert "TRANSFORMERS_OFFLINE=1" in section
