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
