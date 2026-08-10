import hashlib
import uuid
from io import BytesIO
from pathlib import Path

import httpx
import pytest

from app.features.structured_extraction.errors import (
    ExtractionErrorCode,
    ExtractionProcessingError,
)
from app.features.structured_extraction.input_resolver import InputResolver
from app.features.structured_extraction.models import (
    ExtractionTask,
    ExtractionTaskStatus,
)
from app.features.structured_extraction.staging import StagingLayout


@pytest.fixture(autouse=True)
def db() -> None:
    """输入解析单测不依赖 PostgreSQL。"""


def make_task(
    *,
    local_path: str | None,
    remote_url: str | None = None,
) -> ExtractionTask:
    return ExtractionTask(
        caller_id=uuid.uuid4(),
        session_id="session-1",
        file_id="file-1",
        request_fingerprint="a" * 64,
        file_storage_path=local_path,
        file_oss_url=remote_url,
        selected_input_type="local" if local_path else "remote",
        target_path="/allowed/output/sample.md",
        status=ExtractionTaskStatus.QUEUED,
    )


def test_local_input_is_streamed_and_hashed(tmp_path: Path) -> None:
    source = tmp_path / "input" / "sample.txt"
    source.parent.mkdir()
    source.write_bytes("第一行\r\n第二行".encode())
    layout = StagingLayout.for_task(tmp_path / "staging", uuid.uuid4())
    resolver = InputResolver(
        max_input_bytes=1024,
        copy_chunk_bytes=4,
    )

    resolved = resolver.resolve(make_task(local_path=str(source)), layout)

    assert resolved.path.read_bytes() == source.read_bytes()
    assert resolved.path.suffix == ".txt"
    assert resolved.size_bytes == len(source.read_bytes())
    assert resolved.sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert not list(resolved.path.parent.glob("*.part"))


def test_selected_local_input_never_falls_back_to_remote(tmp_path: Path) -> None:
    layout = StagingLayout.for_task(tmp_path / "staging", uuid.uuid4())
    resolver = InputResolver(
        max_input_bytes=1024,
    )

    with pytest.raises(ExtractionProcessingError) as captured:
        resolver.resolve(
            make_task(
                local_path=str(tmp_path / "input" / "missing.txt"),
                remote_url="https://allowed.example/sample.txt",
            ),
            layout,
        )

    assert captured.value.code is ExtractionErrorCode.INPUT_ACCESS_FAILED


def test_input_larger_than_limit_leaves_no_complete_or_partial_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "input" / "large.txt"
    source.parent.mkdir()
    source.write_bytes(b"12345")
    layout = StagingLayout.for_task(tmp_path / "staging", uuid.uuid4())
    resolver = InputResolver(
        max_input_bytes=4,
        copy_chunk_bytes=2,
    )

    with pytest.raises(ExtractionProcessingError) as captured:
        resolver.resolve(make_task(local_path=str(source)), layout)

    assert captured.value.code is ExtractionErrorCode.INPUT_TOO_LARGE
    assert not list(layout.source.parent.glob("*"))


def test_local_path_is_revalidated_by_worker_access(tmp_path: Path) -> None:
    source = tmp_path / "outside" / "sample.txt"
    source.parent.mkdir()
    source.write_text("content", encoding="utf-8")
    layout = StagingLayout.for_task(tmp_path / "staging", uuid.uuid4())
    resolver = InputResolver(
        max_input_bytes=1024,
    )

    resolved = resolver.resolve(make_task(local_path=str(source)), layout)

    assert resolved.path.read_text(encoding="utf-8") == "content"


def test_http_redirect_is_revalidated_before_following(tmp_path: Path) -> None:
    visited: list[str] = []

    def validate_url(url: str) -> str:
        visited.append(url)
        if "blocked.example" in url:
            raise ValueError("blocked")
        return url

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"location": "https://blocked.example/private.txt"},
            request=request,
        )

    resolver = InputResolver(
        max_input_bytes=1024,
        remote_url_validator=validate_url,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    layout = StagingLayout.for_task(tmp_path / "staging", uuid.uuid4())

    with pytest.raises(ExtractionProcessingError) as captured:
        resolver.resolve(
            make_task(
                local_path=None,
                remote_url="https://allowed.example/sample.txt",
            ),
            layout,
        )

    assert captured.value.code is ExtractionErrorCode.INPUT_ACCESS_FAILED
    assert visited == [
        "https://allowed.example/sample.txt",
        "https://blocked.example/private.txt",
    ]


def test_s3_bucket_must_be_in_worker_allowlist(tmp_path: Path) -> None:
    resolver = InputResolver(
        max_input_bytes=1024,
        allowed_s3_buckets=("approved",),
    )
    layout = StagingLayout.for_task(tmp_path / "staging", uuid.uuid4())

    with pytest.raises(ExtractionProcessingError) as captured:
        resolver.resolve(
            make_task(
                local_path=None,
                remote_url="s3://unapproved/sample.txt",
            ),
            layout,
        )

    assert captured.value.code is ExtractionErrorCode.INPUT_ACCESS_FAILED


def test_s3_storage_options_are_passed_to_fsspec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeS3FileSystem:
        def open(self, path: str, mode: str) -> BytesIO:
            assert path == "approved/sample.txt"
            assert mode == "rb"
            return BytesIO(b"s3 content")

    def filesystem(protocol: str, **storage_options: object) -> FakeS3FileSystem:
        assert protocol == "s3"
        captured.update(storage_options)
        return FakeS3FileSystem()

    monkeypatch.setattr(
        "app.features.structured_extraction.input_resolver.fsspec.filesystem",
        filesystem,
    )
    resolver = InputResolver(
        max_input_bytes=1024,
        allowed_s3_buckets=("approved",),
        s3_storage_options={
            "client_kwargs": {
                "endpoint_url": "https://object-storage.example",
                "region_name": "test-region-1",
            },
            "key": "access-key",
            "secret": "secret-key",
        },
    )
    task = make_task(local_path=None, remote_url="s3://approved/sample.txt")

    resolved = resolver.resolve(
        task,
        StagingLayout.for_task(tmp_path / "staging", task.id),
    )

    assert resolved.path.read_bytes() == b"s3 content"
    assert captured == {
        "client_kwargs": {
            "endpoint_url": "https://object-storage.example",
            "region_name": "test-region-1",
        },
        "key": "access-key",
        "secret": "secret-key",
    }


def test_remote_uri_must_not_contain_credentials(tmp_path: Path) -> None:
    resolver = InputResolver(
        max_input_bytes=1024,
        remote_url_validator=lambda url: url,
    )
    layout = StagingLayout.for_task(tmp_path / "staging", uuid.uuid4())

    with pytest.raises(ExtractionProcessingError) as captured:
        resolver.resolve(
            make_task(
                local_path=None,
                remote_url="https://user:secret@allowed.example/sample.txt",
            ),
            layout,
        )

    assert captured.value.code is ExtractionErrorCode.INPUT_ACCESS_FAILED


def test_matching_staged_input_is_reused_without_reopening_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "input" / "sample.txt"
    source.parent.mkdir()
    content = b"stable content"
    source.write_bytes(content)
    task = make_task(local_path=str(source))
    task.input_sha256 = hashlib.sha256(content).hexdigest()
    task.input_size_bytes = len(content)
    layout = StagingLayout.for_task(tmp_path / "staging", task.id)
    layout.prepare()
    staged = layout.source_with_suffix(".txt")
    staged.write_bytes(content)
    source.unlink()
    resolver = InputResolver(
        max_input_bytes=1024,
    )

    resolved = resolver.resolve(task, layout)

    assert resolved.path == staged
    assert resolved.sha256 == task.input_sha256
