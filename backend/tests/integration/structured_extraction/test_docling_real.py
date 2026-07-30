import os
import time
import uuid
import zipfile
from collections.abc import Generator
from pathlib import Path

import httpx
import pytest

from app.core.config import DoclingProfile
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
def docling_client() -> Generator[DoclingHttpAdapter]:
    if os.environ.get("DOCLING_REAL_INTEGRATION") != "1":
        pytest.skip("set DOCLING_REAL_INTEGRATION=1 to run against Docling")
    api_key = os.environ.get("DOCLING_SERVE_API_KEY")
    if not api_key:
        pytest.skip("DOCLING_SERVE_API_KEY is required for real Docling testing")
    base_url = os.environ.get("DOCLING_BASE_URL", "http://localhost:5001")
    client = httpx.Client(timeout=30.0)
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
            profile=DoclingProfile(),
            profile_name="office-default",
            api_key=api_key,
            client=client,
            max_result_bytes=1024 * 1024,
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


@pytest.mark.real_integration
def test_docling_async_round_trip(
    docling_client: DoclingHttpAdapter,
    sample_docx: Path,
    tmp_path: Path,
) -> None:
    submission = docling_client.submit(sample_docx, context())
    status = wait_with_test_deadline(docling_client, submission.external_task_id)

    assert status.state is ExternalTaskState.SUCCEEDED
    artifact = docling_client.fetch_result(
        submission.external_task_id,
        tmp_path / "result.md",
    )
    assert artifact.markdown_path.read_text(encoding="utf-8").strip()


def context() -> ProcessingContext:
    return ProcessingContext(
        task_id=uuid.UUID("018f0000-0000-7000-8000-000000000001"),
        detected_format=DetectedFormat.DOCX,
        profile_name="office-default",
        profile_sha256="a" * 64,
    )


def wait_with_test_deadline(
    docling_client: DoclingHttpAdapter,
    external_task_id: str,
    *,
    deadline_seconds: float = 120.0,
) -> ExternalTaskStatus:
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        status = docling_client.get_status(external_task_id)
        if status.state is not ExternalTaskState.PROCESSING:
            return status
        time.sleep(1.0)
    raise AssertionError("Docling async task did not finish before the test deadline")
