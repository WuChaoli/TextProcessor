import hashlib
import socket
import uuid
from collections.abc import Iterable
from pathlib import Path

import httpx
import pytest

from app.features.markdown_cleaning.input_resolver import (
    AddressResolver,
    InputResolver,
)
from app.features.markdown_cleaning.input_validator import (
    MarkdownInputError,
    MarkdownInputErrorCode,
)
from app.features.markdown_cleaning.staging import StagingLayout
from app.features.markdown_cleaning.state_machine import MarkdownCleaningTaskStatus
from app.features.markdown_cleaning.task_models import MarkdownCleaningTask


def make_task(
    *,
    local_path: str | None,
    remote_url: str | None = None,
    selected_input_type: str | None = None,
    input_sha256: str | None = None,
) -> MarkdownCleaningTask:
    return MarkdownCleaningTask(
        caller_id=uuid.uuid4(),
        session_id="session-1",
        file_id="file-1",
        request_fingerprint="a" * 64,
        file_storage_path=local_path,
        file_oss_url=remote_url,
        selected_input_type=selected_input_type
        or ("local" if local_path is not None else "remote"),
        target_path="/allowed/output/result.md",
        status=MarkdownCleaningTaskStatus.QUEUED,
        input_sha256=input_sha256,
    )


def resolver_for(
    *,
    input_roots: tuple[Path, ...],
    handler: httpx.MockTransport | None = None,
    address_resolver: AddressResolver | None = None,
    max_input_bytes: int = 1024,
) -> InputResolver:
    return InputResolver(
        input_roots=input_roots,
        allowed_http_hosts=("files.internal", "redirect.internal"),
        allowed_http_cidrs=("10.20.0.0/16",),
        max_input_bytes=max_input_bytes,
        copy_chunk_bytes=3,
        connect_timeout_seconds=0.5,
        read_timeout_seconds=0.5,
        address_resolver=address_resolver or allowed_addresses,
        http_client=httpx.Client(transport=handler) if handler is not None else None,
    )


def allowed_addresses(_host: str, _port: int) -> Iterable[str]:
    return ("10.20.30.40",)


def test_local_markdown_is_streamed_to_fixed_original_and_hashed(
    tmp_path: Path,
) -> None:
    source = tmp_path / "input" / "sample.markdown"
    source.parent.mkdir()
    content = "第一行\r\n第二行".encode()
    source.write_bytes(content)
    layout = StagingLayout.for_task(tmp_path / "staging", uuid.uuid4())

    resolved = resolver_for(input_roots=(source.parent,)).resolve(
        make_task(local_path=str(source)), layout
    )

    assert resolved.path == layout.original_source
    assert resolved.path.read_bytes() == content
    assert resolved.size_bytes == len(content)
    assert resolved.sha256 == hashlib.sha256(content).hexdigest()
    assert resolved.source_suffix == ".markdown"
    assert not list(layout.input_dir.glob("*.part"))


def test_selected_local_failure_never_falls_back_to_remote(tmp_path: Path) -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, content=b"remote", request=request)

    layout = StagingLayout.for_task(tmp_path / "staging", uuid.uuid4())
    resolver = resolver_for(
        input_roots=(tmp_path / "input",),
        handler=httpx.MockTransport(handler),
        address_resolver=allowed_addresses,
    )

    with pytest.raises(MarkdownInputError) as captured:
        resolver.resolve(
            make_task(
                local_path=str(tmp_path / "input" / "missing.md"),
                remote_url="https://files.internal/source.md",
                selected_input_type="local",
            ),
            layout,
        )

    assert captured.value.code is MarkdownInputErrorCode.INPUT_NOT_FOUND
    assert requested == []


@pytest.mark.parametrize("relative", ["../outside.md", "nested/../../outside.md"])
def test_local_path_escape_is_rejected(tmp_path: Path, relative: str) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    layout = StagingLayout.for_task(tmp_path / "staging", uuid.uuid4())

    with pytest.raises(MarkdownInputError) as captured:
        resolver_for(input_roots=(allowed,)).resolve(
            make_task(local_path=str(allowed / relative)), layout
        )

    assert captured.value.code is MarkdownInputErrorCode.INPUT_ACCESS_FAILED


def test_symlinked_local_input_escaping_allowlist_is_rejected(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    link = allowed / "source.md"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("当前环境不允许创建文件符号链接")

    with pytest.raises(MarkdownInputError) as captured:
        resolver_for(input_roots=(allowed,)).resolve(
            make_task(local_path=str(link)),
            StagingLayout.for_task(tmp_path / "staging", uuid.uuid4()),
        )

    assert captured.value.code is MarkdownInputErrorCode.INPUT_ACCESS_FAILED


@pytest.mark.parametrize("name", ["source.txt", "source.md.exe", "source"])
def test_local_extension_must_be_markdown(tmp_path: Path, name: str) -> None:
    source = tmp_path / "input" / name
    source.parent.mkdir(exist_ok=True)
    source.write_text("content", encoding="utf-8")

    with pytest.raises(MarkdownInputError) as captured:
        resolver_for(input_roots=(source.parent,)).resolve(
            make_task(local_path=str(source)),
            StagingLayout.for_task(tmp_path / "staging", uuid.uuid4()),
        )

    assert captured.value.code is MarkdownInputErrorCode.UNSUPPORTED_INPUT_FORMAT


def test_remote_credentials_are_rejected_before_request(tmp_path: Path) -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, content=b"content", request=request)

    resolver = resolver_for(
        input_roots=(),
        handler=httpx.MockTransport(handler),
        address_resolver=allowed_addresses,
    )

    with pytest.raises(MarkdownInputError) as captured:
        resolver.resolve(
            make_task(
                local_path=None,
                remote_url="https://user:secret@files.internal/source.md",
            ),
            StagingLayout.for_task(tmp_path / "staging", uuid.uuid4()),
        )

    assert captured.value.code is MarkdownInputErrorCode.INPUT_ACCESS_FAILED
    assert requested == []


def test_http_redirect_revalidates_host_and_all_dns_addresses(tmp_path: Path) -> None:
    visits: list[tuple[str, int]] = []

    def addresses(host: str, port: int) -> Iterable[str]:
        visits.append((host, port))
        if host == "redirect.internal":
            return ("10.20.30.41", "127.0.0.1")
        return ("10.20.30.40",)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"location": "https://redirect.internal/private.md"},
            request=request,
        )

    resolver = resolver_for(
        input_roots=(),
        handler=httpx.MockTransport(handler),
        address_resolver=addresses,
    )

    with pytest.raises(MarkdownInputError) as captured:
        resolver.resolve(
            make_task(
                local_path=None,
                remote_url="https://files.internal/source.md",
            ),
            StagingLayout.for_task(tmp_path / "staging", uuid.uuid4()),
        )

    assert captured.value.code is MarkdownInputErrorCode.INPUT_ACCESS_FAILED
    assert visits[0] == ("files.internal", 443)
    assert visits[-1] == ("redirect.internal", 443)
    assert {host for host, _port in visits} == {
        "files.internal",
        "redirect.internal",
    }


def test_http_dns_resolution_failure_is_safe_error(tmp_path: Path) -> None:
    def fail_resolution(_host: str, _port: int) -> Iterable[str]:
        raise socket.gaierror("dns unavailable")

    resolver = resolver_for(
        input_roots=(),
        handler=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"content", request=request)
        ),
        address_resolver=fail_resolution,
    )

    with pytest.raises(MarkdownInputError) as captured:
        resolver.resolve(
            make_task(
                local_path=None,
                remote_url="https://files.internal/source.md",
            ),
            StagingLayout.for_task(tmp_path / "staging", uuid.uuid4()),
        )

    assert captured.value.code is MarkdownInputErrorCode.INPUT_ACCESS_FAILED
    assert "dns" not in captured.value.safe_message.lower()


def test_http_timeout_is_safe_error_and_removes_partial_file(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("secret endpoint timed out", request=request)

    layout = StagingLayout.for_task(tmp_path / "staging", uuid.uuid4())
    resolver = resolver_for(
        input_roots=(),
        handler=httpx.MockTransport(handler),
        address_resolver=allowed_addresses,
    )

    with pytest.raises(MarkdownInputError) as captured:
        resolver.resolve(
            make_task(
                local_path=None,
                remote_url="https://files.internal/source.md",
            ),
            layout,
        )

    assert captured.value.code is MarkdownInputErrorCode.INPUT_ACCESS_FAILED
    assert not layout.original_source.exists()
    assert not list(layout.input_dir.glob("*.part"))


def test_streamed_size_limit_removes_complete_and_partial_files(tmp_path: Path) -> None:
    source = tmp_path / "input" / "large.md"
    source.parent.mkdir()
    source.write_bytes(b"12345")
    layout = StagingLayout.for_task(tmp_path / "staging", uuid.uuid4())

    with pytest.raises(MarkdownInputError) as captured:
        resolver_for(input_roots=(source.parent,), max_input_bytes=4).resolve(
            make_task(local_path=str(source)), layout
        )

    assert captured.value.code is MarkdownInputErrorCode.INPUT_TOO_LARGE
    assert not layout.original_source.exists()
    assert not list(layout.input_dir.glob("*.part"))


def test_matching_staged_hash_is_reused_without_reopening_selected_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "input" / "source.md"
    source.parent.mkdir()
    content = b"stable content"
    source.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    layout = StagingLayout.for_task(tmp_path / "staging", uuid.uuid4())
    layout.prepare()
    layout.original_source.write_bytes(content)
    source.unlink()

    resolved = resolver_for(input_roots=(source.parent,)).resolve(
        make_task(local_path=str(source), input_sha256=digest), layout
    )

    assert resolved.path == layout.original_source
    assert resolved.sha256 == digest
    assert resolved.size_bytes == len(content)


def test_matching_staged_hash_does_not_bypass_local_allowlist(tmp_path: Path) -> None:
    content = b"stable content"
    digest = hashlib.sha256(content).hexdigest()
    layout = StagingLayout.for_task(tmp_path / "staging", uuid.uuid4())
    layout.prepare()
    layout.original_source.write_bytes(content)

    with pytest.raises(MarkdownInputError) as captured:
        resolver_for(input_roots=(tmp_path / "allowed",)).resolve(
            make_task(
                local_path=str(tmp_path / "outside" / "source.md"),
                input_sha256=digest,
            ),
            layout,
        )

    assert captured.value.code is MarkdownInputErrorCode.INPUT_ACCESS_FAILED


def test_matching_staged_hash_does_not_bypass_remote_credential_policy(
    tmp_path: Path,
) -> None:
    content = b"stable content"
    digest = hashlib.sha256(content).hexdigest()
    layout = StagingLayout.for_task(tmp_path / "staging", uuid.uuid4())
    layout.prepare()
    layout.original_source.write_bytes(content)

    with pytest.raises(MarkdownInputError) as captured:
        resolver_for(input_roots=()).resolve(
            make_task(
                local_path=None,
                remote_url="https://user:secret@files.internal/source.md",
                input_sha256=digest,
            ),
            layout,
        )

    assert captured.value.code is MarkdownInputErrorCode.INPUT_ACCESS_FAILED


def test_stale_staged_hash_is_removed_then_selected_source_failure_is_reported(
    tmp_path: Path,
) -> None:
    source = tmp_path / "input" / "source.md"
    source.parent.mkdir()
    layout = StagingLayout.for_task(tmp_path / "staging", uuid.uuid4())
    layout.prepare()
    layout.original_source.write_bytes(b"tampered")

    with pytest.raises(MarkdownInputError) as captured:
        resolver_for(input_roots=(source.parent,)).resolve(
            make_task(local_path=str(source), input_sha256="a" * 64), layout
        )

    assert captured.value.code is MarkdownInputErrorCode.INPUT_NOT_FOUND
    assert not layout.original_source.exists()
