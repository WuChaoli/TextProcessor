from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_dockerfile_wraps_pinned_docling_image() -> None:
    content = (ROOT / "services/docling_service/Dockerfile").read_text(
        encoding="utf-8"
    )

    assert "ARG DOCLING_BASE_IMAGE" in content
    assert "FROM ${DOCLING_BASE_IMAGE}" in content
    assert 'ENTRYPOINT ["python", "/opt/textprocessor-docling/process_manager.py"]' in content
    assert "HEALTHCHECK" not in content
    assert "USER 1001:0" in content


def test_runtime_assets_do_not_modify_docling_source() -> None:
    dockerfile = (ROOT / "services/docling_service/Dockerfile").read_text(
        encoding="utf-8"
    )

    assert "process_manager.py" in dockerfile
    assert "healthcheck.py" in dockerfile
    assert "apt-get" not in dockerfile
    assert "pip install" not in dockerfile


def test_compose_has_one_docling_container_and_shared_redis() -> None:
    content = (ROOT / "compose.docling.yml").read_text(encoding="utf-8")

    assert "  docling-api:" in content
    assert "  docling-worker:" not in content
    assert "  docling-redis:" not in content
    assert "docling-redis-data" not in content
    assert "redis://redis:6379/1" in content
    assert "DOCLING_SERVE_ENG_KIND: rq" in content
    assert "services/docling_service/Dockerfile" in content
    assert "/opt/app-root/src/.cache" in content


def test_celery_remains_on_redis_db_zero() -> None:
    content = (ROOT / "compose.yml").read_text(encoding="utf-8")

    assert content.count("CELERY_BROKER_URL=redis://redis:6379/0") == 3


def test_local_override_has_no_removed_docling_services() -> None:
    content = (ROOT / "compose.override.yml").read_text(encoding="utf-8")

    assert "  docling-worker:" not in content
    assert "  docling-redis:" not in content


def test_environment_template_has_no_docling_redis_password() -> None:
    content = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "DOCLING_BASE_IMAGE=" in content
    assert "DOCKER_IMAGE_DOCLING=" in content
    assert "DOCLING_REDIS_PASSWORD" not in content
