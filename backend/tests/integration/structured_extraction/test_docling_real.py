import hashlib
import json
import os
import time
import uuid
from collections.abc import Generator
from pathlib import Path

import httpx
import pytest

from app.core.config import ExtractionWorkerSettings, settings
from app.features.structured_extraction.adapters.docling import DoclingHttpAdapter
from app.features.structured_extraction.format_detector import FormatDetector
from app.features.structured_extraction.office_inspector import OfficeDocumentInspector
from app.features.structured_extraction.router import ProcessorRouter
from app.features.structured_extraction.worker_models import (
    DetectedFormat,
    ExternalTaskState,
    ExternalTaskStatus,
    ProcessingContext,
    ProcessorName,
)

_REQUIRED_SAMPLES = {
    "docx": DetectedFormat.DOCX,
    "xlsx": DetectedFormat.XLSX,
    "html": DetectedFormat.HTML,
    "epub": DetectedFormat.EPUB,
}


@pytest.fixture(autouse=True)
def db() -> None:
    """外部 Docling 契约测试不依赖 PostgreSQL。"""


@pytest.fixture(scope="module")
def worker_settings() -> ExtractionWorkerSettings:
    if os.environ.get("DOCLING_REAL_INTEGRATION") != "1":
        pytest.skip("set DOCLING_REAL_INTEGRATION=1 to run against Docling")
    configured = settings.EXTRACTION_WORKER
    if configured.docling_base_url is None or not configured.docling_api_key:
        pytest.fail("Docling real integration requires configured base URL and API key")
    return configured


@pytest.fixture(scope="module")
def docling_samples() -> dict[str, Path]:
    raw_samples = os.environ.get("DOCLING_REAL_SAMPLE_PATHS")
    if raw_samples is None:
        pytest.fail("Docling real integration requires DOCLING_REAL_SAMPLE_PATHS")
    try:
        parsed_samples = json.loads(raw_samples)
    except json.JSONDecodeError:
        pytest.fail("DOCLING_REAL_SAMPLE_PATHS must be a JSON object")
    if not isinstance(parsed_samples, dict):
        pytest.fail("DOCLING_REAL_SAMPLE_PATHS must be a JSON object")

    samples: dict[str, Path] = {}
    for format_name in _REQUIRED_SAMPLES:
        configured_path = parsed_samples.get(format_name)
        if not isinstance(configured_path, str) or not configured_path:
            pytest.fail(f"Docling real integration is missing the {format_name} sample")
        source = Path(configured_path)
        if source.suffix.lower() != f".{format_name}" or not source.is_file():
            pytest.fail(f"Docling {format_name} sample is not readable")
        samples[format_name] = source
    return samples


@pytest.fixture(scope="module")
def docling_expectations() -> dict[str, tuple[str, ...]]:
    return load_expectations("DOCLING_REAL_EXPECTATIONS", _REQUIRED_SAMPLES)


@pytest.fixture(scope="module")
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
            health = client.get(f"{base_url}/health", headers={"X-API-Key": api_key})
        except httpx.RequestError as error:
            pytest.fail(f"Docling is unreachable: {error.__class__.__name__}")
        if health.status_code != 200:
            pytest.fail(f"Docling health check returned HTTP {health.status_code}")
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


@pytest.mark.real_integration
@pytest.mark.parametrize(("format_name", "detected_format"), _REQUIRED_SAMPLES.items())
def test_docling_async_round_trip(
    format_name: str,
    detected_format: DetectedFormat,
    docling_client: DoclingHttpAdapter,
    docling_samples: dict[str, Path],
    docling_expectations: dict[str, tuple[str, ...]],
    tmp_path: Path,
    worker_settings: ExtractionWorkerSettings,
) -> None:
    source = docling_samples[format_name]
    if detected_format is DetectedFormat.DOCX:
        document = FormatDetector().detect(source)
        inspection = OfficeDocumentInspector().inspect_docx(source)
        decision = ProcessorRouter(
            production_formats=("docx",),
            docx_visual_complexity_threshold=(
                worker_settings.docx_visual_complexity_threshold
            ),
        ).route(document, inspection)
        assert decision.processor is ProcessorName.DOCLING
        assert decision.reasons == ("ordinary_docx",)

    profile_json = json.dumps(
        worker_settings.docling_profile.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    processing_context = ProcessingContext(
        task_id=uuid.uuid4(),
        detected_format=detected_format,
        profile_name=worker_settings.docling_profile_name,
        profile_sha256=hashlib.sha256(profile_json.encode()).hexdigest(),
    )

    submission = docling_client.submit(source, processing_context)
    status = wait_with_test_deadline(
        docling_client,
        submission.external_task_id,
        deadline_seconds=min(worker_settings.processing_deadline_seconds, 120),
        poll_interval_seconds=worker_settings.poll_interval_seconds,
    )

    assert status.state is ExternalTaskState.SUCCEEDED
    artifact = docling_client.fetch_result(
        submission.external_task_id,
        tmp_path / f"{format_name}.md",
    )
    result = artifact.markdown_path.read_bytes()
    assert result and not result.startswith(b"\xef\xbb\xbf")
    markdown = result.decode("utf-8").strip()
    assert markdown
    normalized = normalize_text(markdown)
    for expected in docling_expectations[format_name]:
        assert normalize_text(expected) in normalized
    assert artifact.profile_name == worker_settings.docling_profile_name
    assert artifact.profile_sha256 == processing_context.profile_sha256


def load_expectations(
    environment_name: str,
    required_samples: dict[str, DetectedFormat],
) -> dict[str, tuple[str, ...]]:
    raw_expectations = os.environ.get(environment_name)
    if raw_expectations is None:
        pytest.fail(f"Docling real integration requires {environment_name}")
    try:
        parsed_expectations = json.loads(raw_expectations)
    except json.JSONDecodeError:
        pytest.fail(f"{environment_name} must be a JSON object")
    if not isinstance(parsed_expectations, dict):
        pytest.fail(f"{environment_name} must be a JSON object")

    expectations: dict[str, tuple[str, ...]] = {}
    for format_name in required_samples:
        configured = parsed_expectations.get(format_name)
        if not isinstance(configured, list) or not configured or not all(
            isinstance(value, str) and value.strip() for value in configured
        ):
            pytest.fail(f"Docling {format_name} expectations must be non-empty strings")
        expectations[format_name] = tuple(configured)
    return expectations


def normalize_text(value: str) -> str:
    return " ".join(value.casefold().split())


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
