import uuid
from pathlib import Path

import httpx
import pytest

from app.core.config import MinerUProfile
from app.features.structured_extraction.adapters.mineru import MinerUHttpAdapter
from app.features.structured_extraction.errors import (
    ExtractionErrorCode,
    ExtractionProcessingError,
)
from app.features.structured_extraction.worker_models import (
    DetectedFormat,
    ExternalTaskState,
    ProcessingContext,
    ProcessorName,
)


@pytest.fixture(autouse=True)
def db() -> None:
    """适配器单测不需要 PostgreSQL。"""


class CountingStream(httpx.SyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.yield_count = 0

    def __iter__(self):
        for chunk in self._chunks:
            self.yield_count += 1
            yield chunk

    def close(self) -> None:
        return None


def context() -> ProcessingContext:
    return ProcessingContext(
        task_id=uuid.UUID("018f0000-0000-7000-8000-000000000001"),
        detected_format=DetectedFormat.PDF,
        profile_name="default",
        profile_sha256="a" * 64,
    )


def test_submit_sends_file_and_lowercase_profile_fields(tmp_path: Path) -> None:
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"%PDF")
    captured_body = b""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_body
        assert request.url.path == "/tasks"
        assert request.headers["x-api-key"] == "secret"
        captured_body = request.read()
        return httpx.Response(202, json={"task_id": "mineru-1"})

    adapter = MinerUHttpAdapter(
        base_url="http://mineru.internal",
        profile=MinerUProfile(),
        profile_name="default",
        api_key="secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_result_bytes=1024,
    )

    submission = adapter.submit(source, context())

    assert submission.external_task_id == "mineru-1"
    assert submission.processor_name is ProcessorName.MINERU
    assert b'name="files"; filename="sample.pdf"' in captured_body
    assert b'name="return_md"' in captured_body
    assert b"\r\ntrue\r\n" in captured_body
    assert b'name="return_images"' in captured_body
    assert b"\r\nfalse\r\n" in captured_body


@pytest.mark.parametrize("status", ["queued", "pending", "processing", "running"])
def test_maps_processing_statuses(status: str) -> None:
    adapter = make_adapter(lambda request: httpx.Response(200, json={"status": status}))

    assert adapter.get_status("task-1").state is ExternalTaskState.PROCESSING


def test_maps_completed_and_failed_statuses() -> None:
    responses = iter(
        [
            httpx.Response(200, json={"status": "completed"}),
            httpx.Response(
                200,
                json={
                    "status": "failed",
                    "error": "  parse \n failed  ",
                },
            ),
        ]
    )
    adapter = make_adapter(lambda request: next(responses))

    assert adapter.get_status("task-1").state is ExternalTaskState.SUCCEEDED
    failed = adapter.get_status("task-2")
    assert failed.state is ExternalTaskState.FAILED
    assert failed.safe_error_message == "parse failed"


def test_rejects_unknown_status() -> None:
    adapter = make_adapter(
        lambda request: httpx.Response(200, json={"status": "mystery"})
    )

    with pytest.raises(ExtractionProcessingError) as captured:
        adapter.get_status("task-1")

    assert captured.value.code is ExtractionErrorCode.INVALID_PROCESSOR_OUTPUT


def test_fetches_unique_result_without_assuming_original_stem(
    tmp_path: Path,
) -> None:
    adapter = make_adapter(
        lambda request: httpx.Response(
            200,
            json={
                "backend": "hybrid-engine",
                "version": "1.2.3",
                "results": {
                    "service-generated-name": {
                        "md_content": "# 提取结果\n",
                        "content_list": [],
                    }
                },
            },
        )
    )
    destination = tmp_path / "processor" / "raw-result.md"

    artifact = adapter.fetch_result("task-1", destination)

    assert destination.read_text(encoding="utf-8") == "# 提取结果\n"
    assert artifact.processor_name is ProcessorName.MINERU
    assert artifact.processor_version == "1.2.3"
    assert artifact.profile_name == "default"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(
            200, content=b"not-json", headers={"content-type": "text/plain"}
        ),
        httpx.Response(
            200,
            json={
                "backend": "hybrid",
                "version": "1",
                "results": {"one": {"md_content": ""}},
            },
        ),
        httpx.Response(
            200,
            json={
                "backend": "hybrid",
                "version": "1",
                "results": {
                    "one": {"md_content": "a"},
                    "two": {"md_content": "b"},
                },
            },
        ),
    ],
)
def test_rejects_invalid_result(response: httpx.Response, tmp_path: Path) -> None:
    adapter = make_adapter(lambda request: response, max_result_bytes=1024)

    with pytest.raises(ExtractionProcessingError) as captured:
        adapter.fetch_result("task-1", tmp_path / "result.md")

    assert captured.value.code is ExtractionErrorCode.INVALID_PROCESSOR_OUTPUT


def test_rejects_oversized_result(tmp_path: Path) -> None:
    adapter = make_adapter(
        lambda request: httpx.Response(
            200,
            json={
                "backend": "hybrid",
                "version": "1",
                "results": {"one": {"md_content": "large result"}},
            },
        ),
        max_result_bytes=8,
    )

    with pytest.raises(ExtractionProcessingError) as captured:
        adapter.fetch_result("task-1", tmp_path / "result.md")

    assert captured.value.code is ExtractionErrorCode.INVALID_PROCESSOR_OUTPUT


def test_rejects_oversized_streamed_result_without_reading_full_body(
    tmp_path: Path,
) -> None:
    stream = CountingStream([b'{"a":"', b"123456", b'"}'])
    adapter = make_adapter(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=stream,
        ),
        max_result_bytes=10,
    )

    with pytest.raises(ExtractionProcessingError) as captured:
        adapter.fetch_result("task-1", tmp_path / "result.md")

    assert captured.value.code is ExtractionErrorCode.INVALID_PROCESSOR_OUTPUT
    assert stream.yield_count == 2


def test_submit_timeout_is_uncertain_and_not_marked_transient(
    tmp_path: Path,
) -> None:
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"%PDF")

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("response lost", request=request)

    adapter = make_adapter(timeout)

    with pytest.raises(ExtractionProcessingError) as captured:
        adapter.submit(source, context())

    assert captured.value.code is ExtractionErrorCode.PROCESSOR_SUBMISSION_UNCERTAIN
    assert captured.value.transient is False


def make_adapter(
    handler: httpx.MockTransportHandler,
    *,
    max_result_bytes: int = 4096,
) -> MinerUHttpAdapter:
    return MinerUHttpAdapter(
        base_url="http://mineru.internal",
        profile=MinerUProfile(),
        profile_name="default",
        api_key=None,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_result_bytes=max_result_bytes,
    )
