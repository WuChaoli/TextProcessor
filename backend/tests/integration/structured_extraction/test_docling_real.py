import hashlib
import json
import os
import time
import uuid
import zipfile
from collections.abc import Generator
from pathlib import Path

import httpx
import pytest

from app.core.config import ExtractionWorkerSettings, settings
from app.features.structured_extraction.adapters.docling import DoclingHttpAdapter
from app.features.structured_extraction.worker_models import (
    DetectedFormat,
    ExternalTaskState,
    ExternalTaskStatus,
    ProcessingContext,
)


@pytest.fixture(autouse=True)
def db() -> None:
    """外部 Docling 契约测试不依赖 PostgreSQL。"""


@pytest.fixture
def worker_settings() -> ExtractionWorkerSettings:
    if os.environ.get("DOCLING_REAL_INTEGRATION") != "1":
        pytest.skip("set DOCLING_REAL_INTEGRATION=1 to run against Docling")
    configured = settings.EXTRACTION_WORKER
    if configured.docling_base_url is None or not configured.docling_api_key:
        pytest.skip(
            "EXTRACTION_WORKER docling_base_url and docling_api_key are required"
        )
    return configured


@pytest.fixture
def docling_client(
    worker_settings: ExtractionWorkerSettings,
) -> Generator[DoclingHttpAdapter]:
    base_url = str(worker_settings.docling_base_url).rstrip("/")
    api_key = worker_settings.docling_api_key
    assert api_key is not None
    client = httpx.Client(
        timeout=httpx.Timeout(
            connect=worker_settings.connect_timeout_seconds,
            read=worker_settings.read_timeout_seconds,
            write=worker_settings.read_timeout_seconds,
            pool=worker_settings.connect_timeout_seconds,
        )
    )
    try:
        try:
            health = client.get(
                f"{base_url.rstrip('/')}/health", headers={"X-API-Key": api_key}
            )
        except httpx.RequestError as error:
            pytest.skip(f"Docling is unreachable: {error.__class__.__name__}")
        if health.status_code != 200:
            pytest.skip(f"Docling health check returned HTTP {health.status_code}")
        yield DoclingHttpAdapter(
            base_url=base_url,
            profile=worker_settings.docling_profile,
            profile_name=worker_settings.docling_profile_name,
            api_key=api_key,
            client=client,
            max_result_bytes=worker_settings.max_output_bytes,
        )
    finally:
        client.close()


@pytest.fixture
def sample_docx(tmp_path: Path) -> Path:
    source = tmp_path / "sample.docx"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            """<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>
<Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\">
  <Default Extension=\"rels\" ContentType=\"application/vnd.openxmlformats-package.relationships+xml\"/>
  <Default Extension=\"xml\" ContentType=\"application/xml\"/>
  <Override PartName=\"/word/document.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml\"/>
</Types>""",
        )
        archive.writestr(
            "_rels/.rels",
            """<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>
<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\">
  <Relationship Id=\"rId1\" Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument\" Target=\"word/document.xml\"/>
</Relationships>""",
        )
        archive.writestr(
            "word/document.xml",
            """<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>
<w:document xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\">
  <w:body><w:p><w:r><w:t>Docling integration sample</w:t></w:r></w:p></w:body>
</w:document>""",
        )
    return source


@pytest.fixture
def processing_context(
    worker_settings: ExtractionWorkerSettings,
) -> ProcessingContext:
    profile_json = json.dumps(
        worker_settings.docling_profile.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return ProcessingContext(
        task_id=uuid.UUID("018f0000-0000-7000-8000-000000000001"),
        detected_format=DetectedFormat.DOCX,
        profile_name=worker_settings.docling_profile_name,
        profile_sha256=hashlib.sha256(profile_json.encode()).hexdigest(),
    )


@pytest.mark.real_integration
def test_docling_async_round_trip(
    docling_client: DoclingHttpAdapter,
    sample_docx: Path,
    tmp_path: Path,
    processing_context: ProcessingContext,
    worker_settings: ExtractionWorkerSettings,
) -> None:
    submission = docling_client.submit(sample_docx, processing_context)
    status = wait_with_test_deadline(
        docling_client,
        submission.external_task_id,
        deadline_seconds=min(worker_settings.processing_deadline_seconds, 120),
        poll_interval_seconds=worker_settings.poll_interval_seconds,
    )

    assert status.state is ExternalTaskState.SUCCEEDED
    artifact = docling_client.fetch_result(
        submission.external_task_id,
        tmp_path / "result.md",
    )
    assert artifact.markdown_path.read_text(encoding="utf-8").strip()
    assert artifact.profile_name == worker_settings.docling_profile_name
    assert artifact.profile_sha256 == processing_context.profile_sha256


def wait_with_test_deadline(
    docling_client: DoclingHttpAdapter,
    external_task_id: str,
    *,
    deadline_seconds: float,
    poll_interval_seconds: float,
) -> ExternalTaskStatus:
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        status = docling_client.get_status(external_task_id)
        if status.state is not ExternalTaskState.PROCESSING:
            return status
        time.sleep(poll_interval_seconds)
    raise AssertionError("Docling async task did not finish before the test deadline")
