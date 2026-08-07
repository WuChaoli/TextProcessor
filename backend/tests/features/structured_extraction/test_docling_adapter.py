import uuid
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from app.core.config import DoclingProfile
from app.features.structured_extraction.adapters.docling import DoclingHttpAdapter
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
        detected_format=DetectedFormat.DOCX,
        profile_name="office-default",
        profile_sha256="a" * 64,
    )


def test_submit_uses_v1_async_file_contract_and_api_key(tmp_path: Path) -> None:
    source = tmp_path / "sample.docx"
    source.write_bytes(b"PK")
    captured_body = b""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_body
        assert request.method == "POST"
        assert request.url.path == "/v1/convert/file/async"
        assert request.headers["x-api-key"] == "secret"
        captured_body = request.read()
        return httpx.Response(
            200,
            json={
                "task_id": "docling-1",
                "task_type": "convert",
                "task_status": "pending",
            },
        )

    adapter = make_adapter(handler, api_key="secret")

    submission = adapter.submit(source, context())

    assert submission.external_task_id == "docling-1"
    assert submission.processor_name is ProcessorName.DOCLING
    assert b'name="files"; filename="sample.docx"' in captured_body
    assert b'name="to_formats"' in captured_body
    assert b"\r\nmd\r\n" in captured_body
    assert b'name="image_export_mode"' in captured_body
    assert b"\r\nplaceholder\r\n" in captured_body
    assert b'name="ocr"' in captured_body
    assert b"\r\nfalse\r\n" in captured_body
    assert b"do_ocr" not in captured_body


def test_submit_explicitly_allows_the_detected_input_format(tmp_path: Path) -> None:
    source = tmp_path / "sample.epub"
    source.write_bytes(b"PK")
    captured_body = b""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_body
        captured_body = request.read()
        return httpx.Response(
            200,
            json={
                "task_id": "docling-epub-1",
                "task_type": "convert",
                "task_status": "pending",
            },
        )

    make_adapter(handler).submit(source, context())

    assert b'name="from_formats"' in captured_body
    assert b"\r\nepub\r\n" in captured_body
    assert b"Content-Type: application/epub+zip" in captured_body


@pytest.mark.parametrize("status", ["pending", "started"])
def test_maps_pending_and_started_to_processing(status: str) -> None:
    adapter = make_adapter(
        lambda request: httpx.Response(
            200,
            json={
                "task_id": "docling-1",
                "task_type": "convert",
                "task_status": status,
            },
        )
    )

    assert adapter.get_status("docling-1").state is ExternalTaskState.PROCESSING


def test_maps_success_and_failure_statuses() -> None:
    responses = iter(
        [
            httpx.Response(
                200,
                json={
                    "task_id": "docling-1",
                    "task_type": "convert",
                    "task_status": "success",
                },
            ),
            httpx.Response(
                200,
                json={
                    "task_id": "docling-2",
                    "task_type": "convert",
                    "task_status": "failure",
                    "error_message": "  conversion \n failed  ",
                },
            ),
        ]
    )
    adapter = make_adapter(lambda request: next(responses))

    assert adapter.get_status("docling-1").state is ExternalTaskState.SUCCEEDED
    failed = adapter.get_status("docling-2")
    assert failed.state is ExternalTaskState.FAILED
    assert failed.safe_error_code is ExtractionErrorCode.PROCESSING_FAILED
    assert failed.safe_error_message == "conversion failed"


def test_rejects_status_for_a_different_task() -> None:
    adapter = make_adapter(
        lambda request: httpx.Response(
            200,
            json={
                "task_id": "another-task",
                "task_type": "convert",
                "task_status": "success",
            },
        )
    )

    with pytest.raises(ExtractionProcessingError) as captured:
        adapter.get_status("docling-1")

    assert captured.value.code is ExtractionErrorCode.INVALID_PROCESSOR_OUTPUT
    assert captured.value.external_task_id == "docling-1"


@pytest.mark.parametrize("status_code", [401, 404])
def test_status_rejects_unauthorized_or_missing_task(status_code: int) -> None:
    adapter = make_adapter(lambda request: httpx.Response(status_code))

    with pytest.raises(ExtractionProcessingError) as captured:
        adapter.get_status("docling-1")

    assert captured.value.code is ExtractionErrorCode.PROCESSING_FAILED
    assert captured.value.external_task_id == "docling-1"


def test_fetches_markdown_from_document_result(tmp_path: Path) -> None:
    adapter = make_adapter(
        lambda request: httpx.Response(
            200,
            json={
                "status": "success",
                "document": {"md_content": "# 提取结果\n"},
            },
        )
    )
    destination = tmp_path / "processor" / "raw-result.md"

    artifact = adapter.fetch_result("docling-1", destination)

    assert destination.read_text(encoding="utf-8") == "# 提取结果\n"
    assert artifact.processor_name is ProcessorName.DOCLING
    assert artifact.processor_version is None
    assert artifact.profile_name == "office-default"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404),
        httpx.Response(
            200,
            content=b"not-json",
            headers={"content-type": "text/plain"},
        ),
        httpx.Response(
            200,
            json={"status": "success", "document": {"md_content": ""}},
        ),
    ],
)
def test_rejects_expired_or_invalid_result(
    response: httpx.Response,
    tmp_path: Path,
) -> None:
    adapter = make_adapter(lambda request: response)

    with pytest.raises(ExtractionProcessingError) as captured:
        adapter.fetch_result("docling-1", tmp_path / "result.md")

    expected = (
        ExtractionErrorCode.PROCESSING_FAILED
        if response.status_code == 404
        else ExtractionErrorCode.INVALID_PROCESSOR_OUTPUT
    )
    assert captured.value.code is expected
    assert captured.value.external_task_id == "docling-1"


def test_rejects_oversized_markdown(tmp_path: Path) -> None:
    adapter = make_adapter(
        lambda request: httpx.Response(
            200,
            json={"status": "success", "document": {"md_content": "too long"}},
        ),
        max_result_bytes=4,
    )

    with pytest.raises(ExtractionProcessingError) as captured:
        adapter.fetch_result("docling-1", tmp_path / "result.md")

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
        adapter.fetch_result("docling-1", tmp_path / "result.md")

    assert captured.value.code is ExtractionErrorCode.INVALID_PROCESSOR_OUTPUT
    assert captured.value.external_task_id == "docling-1"
    assert stream.yield_count == 2


def test_submit_timeout_is_uncertain(tmp_path: Path) -> None:
    source = tmp_path / "sample.docx"
    source.write_bytes(b"PK")

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("response lost", request=request)

    adapter = make_adapter(timeout)

    with pytest.raises(ExtractionProcessingError) as captured:
        adapter.submit(source, context())

    assert captured.value.code is ExtractionErrorCode.PROCESSOR_SUBMISSION_UNCERTAIN
    assert captured.value.transient is False


def make_adapter(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    api_key: str | None = None,
    max_result_bytes: int = 4096,
) -> DoclingHttpAdapter:
    return DoclingHttpAdapter(
        base_url="http://docling.internal",
        profile=DoclingProfile(),
        profile_name="office-default",
        api_key=api_key,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_result_bytes=max_result_bytes,
    )
