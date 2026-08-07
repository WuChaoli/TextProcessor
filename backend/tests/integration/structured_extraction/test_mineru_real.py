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
from app.features.structured_extraction.adapters.mineru import MinerUHttpAdapter
from app.features.structured_extraction.worker_models import (
    DetectedFormat,
    ExternalTaskState,
    ExternalTaskStatus,
    ProcessingContext,
)

_REQUIRED_SAMPLES = {
    "pdf": DetectedFormat.PDF,
    "png": DetectedFormat.IMAGE,
    "jpg": DetectedFormat.IMAGE,
    "pptx": DetectedFormat.PPTX,
}
_SAMPLE_SUFFIXES = {
    "pdf": {".pdf"},
    "png": {".png"},
    "jpg": {".jpg", ".jpeg"},
    "pptx": {".pptx"},
}


@pytest.fixture(autouse=True)
def db() -> None:
    """外部 MinerU 契约测试不依赖 PostgreSQL。"""


@pytest.fixture(scope="module")
def worker_settings() -> ExtractionWorkerSettings:
    if os.environ.get("MINERU_REAL_INTEGRATION") != "1":
        pytest.skip("set MINERU_REAL_INTEGRATION=1 to run against MinerU")
    configured = settings.EXTRACTION_WORKER
    if configured.mineru_base_url is None:
        pytest.fail("MinerU real integration requires a configured base URL")
    return configured


@pytest.fixture(scope="module")
def mineru_samples() -> dict[str, Path]:
    raw_samples = os.environ.get("MINERU_REAL_SAMPLE_PATHS")
    if raw_samples is None:
        pytest.fail("MinerU real integration requires MINERU_REAL_SAMPLE_PATHS")
    try:
        parsed_samples = json.loads(raw_samples)
    except json.JSONDecodeError:
        pytest.fail("MINERU_REAL_SAMPLE_PATHS must be a JSON object")
    if not isinstance(parsed_samples, dict):
        pytest.fail("MINERU_REAL_SAMPLE_PATHS must be a JSON object")

    samples: dict[str, Path] = {}
    for format_name in _REQUIRED_SAMPLES:
        configured_path = parsed_samples.get(format_name)
        if not isinstance(configured_path, str) or not configured_path:
            pytest.fail(f"MinerU real integration is missing the {format_name} sample")
        source = Path(configured_path)
        if (
            source.suffix.lower() not in _SAMPLE_SUFFIXES[format_name]
            or not source.is_file()
        ):
            pytest.fail(f"MinerU {format_name} sample is not readable")
        samples[format_name] = source
    return samples


@pytest.fixture(scope="module")
def mineru_expectations() -> dict[str, tuple[str, ...]]:
    return load_expectations("MINERU_REAL_EXPECTATIONS", _REQUIRED_SAMPLES)


@pytest.fixture(scope="module")
def mineru_client(
    worker_settings: ExtractionWorkerSettings,
) -> Generator[MinerUHttpAdapter]:
    base_url = str(worker_settings.mineru_base_url).rstrip("/")
    client = httpx.Client(
        timeout=httpx.Timeout(
            connect=worker_settings.connect_timeout_seconds,
            read=worker_settings.read_timeout_seconds,
            write=worker_settings.read_timeout_seconds,
            pool=worker_settings.connect_timeout_seconds,
        )
    )
    headers = (
        {"X-API-Key": worker_settings.mineru_api_key}
        if worker_settings.mineru_api_key
        else {}
    )
    try:
        try:
            health = client.get(f"{base_url}/health", headers=headers)
        except httpx.RequestError as error:
            pytest.fail(f"MinerU is unreachable: {error.__class__.__name__}")
        if health.status_code != 200:
            pytest.fail(f"MinerU health check returned HTTP {health.status_code}")
        yield MinerUHttpAdapter(
            base_url=base_url,
            profile=worker_settings.mineru_profile,
            profile_name=worker_settings.mineru_profile_name,
            api_key=worker_settings.mineru_api_key,
            client=client,
            max_result_bytes=worker_settings.max_output_bytes,
        )
    finally:
        client.close()


@pytest.mark.real_integration
@pytest.mark.parametrize(("format_name", "detected_format"), _REQUIRED_SAMPLES.items())
def test_mineru_async_round_trip(
    format_name: str,
    detected_format: DetectedFormat,
    mineru_client: MinerUHttpAdapter,
    mineru_samples: dict[str, Path],
    mineru_expectations: dict[str, tuple[str, ...]],
    tmp_path: Path,
    worker_settings: ExtractionWorkerSettings,
) -> None:
    profile_json = json.dumps(
        worker_settings.mineru_profile.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    processing_context = ProcessingContext(
        task_id=uuid.uuid4(),
        detected_format=detected_format,
        profile_name=worker_settings.mineru_profile_name,
        profile_sha256=hashlib.sha256(profile_json.encode()).hexdigest(),
    )

    submission = mineru_client.submit(mineru_samples[format_name], processing_context)
    status = wait_with_test_deadline(
        mineru_client,
        submission.external_task_id,
        deadline_seconds=min(worker_settings.processing_deadline_seconds, 120),
        poll_interval_seconds=worker_settings.poll_interval_seconds,
    )

    assert status.state is ExternalTaskState.SUCCEEDED
    artifact = mineru_client.fetch_result(
        submission.external_task_id,
        tmp_path / f"{format_name}.md",
    )
    result = artifact.markdown_path.read_bytes()
    assert result and not result.startswith(b"\xef\xbb\xbf")
    markdown = result.decode("utf-8").strip()
    assert markdown
    normalized = normalize_text(markdown)
    for expected in mineru_expectations[format_name]:
        assert normalize_text(expected) in normalized
    assert artifact.profile_name == worker_settings.mineru_profile_name
    assert artifact.profile_sha256 == processing_context.profile_sha256
    assert artifact.processor_version


def load_expectations(
    environment_name: str,
    required_samples: dict[str, DetectedFormat],
) -> dict[str, tuple[str, ...]]:
    raw_expectations = os.environ.get(environment_name)
    if raw_expectations is None:
        pytest.fail(f"MinerU real integration requires {environment_name}")
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
            pytest.fail(f"MinerU {format_name} expectations must be non-empty strings")
        expectations[format_name] = tuple(configured)
    return expectations


def normalize_text(value: str) -> str:
    return " ".join(value.casefold().split())


def wait_with_test_deadline(
    mineru_client: MinerUHttpAdapter,
    external_task_id: str,
    *,
    deadline_seconds: float,
    poll_interval_seconds: float,
) -> ExternalTaskStatus:
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        status = mineru_client.get_status(external_task_id)
        if status.state is not ExternalTaskState.PROCESSING:
            return status
        time.sleep(poll_interval_seconds)
    raise AssertionError("MinerU async task did not finish before the test deadline")
