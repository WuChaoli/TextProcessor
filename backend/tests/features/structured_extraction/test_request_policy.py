from pathlib import Path

import pytest

from app.features.structured_extraction.errors import (
    ExtractionDomainError,
    ExtractionErrorCode,
)
from app.features.structured_extraction.request_policy import RequestPolicy
from app.features.structured_extraction.schemas import ExtractionTaskCreate


def make_policy(
    input_root: Path,
    output_root: Path,
    *,
    resolved_addresses: tuple[str, ...] = ("10.20.0.8",),
) -> RequestPolicy:
    return RequestPolicy(
        input_roots=(input_root,),
        output_roots=(output_root,),
        allowed_http_hosts=("files.internal",),
        allowed_http_cidrs=("10.20.0.0/16",),
        max_input_bytes=1024,
        resolver=lambda _host, _port: resolved_addresses,
    )


def test_validate_request_prefers_existing_local_input(tmp_path: Path) -> None:
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    input_root.mkdir()
    output_root.mkdir()
    source = input_root / "sample.txt"
    source.write_text("hello", encoding="utf-8")
    policy = make_policy(input_root, output_root)

    result = policy.validate_request(
        ExtractionTaskCreate(
            sessionId="session-1",
            fileId="file-1",
            fileStoragePath=str(source),
            fileOssUrl="https://files.internal/sample.txt",
            targetPath=str(output_root / "sample.md"),
        )
    )

    assert result.selected_input_type == "local"
    assert result.file_storage_path == str(source.resolve())
    assert result.file_oss_url == "https://files.internal/sample.txt"
    assert result.target_path == str((output_root / "sample.md").resolve())


def test_local_input_must_exist_and_stay_under_allowed_root(tmp_path: Path) -> None:
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    input_root.mkdir()
    output_root.mkdir()
    policy = make_policy(input_root, output_root)

    for path in (input_root / "missing.txt", tmp_path / "private.txt"):
        with pytest.raises(ExtractionDomainError) as raised:
            policy.validate_local_input(str(path))
        assert raised.value.code is ExtractionErrorCode.INPUT_PATH_NOT_ALLOWED


def test_local_input_honors_size_limit(tmp_path: Path) -> None:
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    input_root.mkdir()
    output_root.mkdir()
    source = input_root / "large.txt"
    source.write_bytes(b"x" * 1025)

    with pytest.raises(ExtractionDomainError) as raised:
        make_policy(input_root, output_root).validate_local_input(str(source))

    assert raised.value.code is ExtractionErrorCode.INPUT_PATH_NOT_ALLOWED


def test_output_must_be_absolute_markdown_under_allowed_root(
    tmp_path: Path,
) -> None:
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    input_root.mkdir()
    output_root.mkdir()
    policy = make_policy(input_root, output_root)

    invalid_paths = (
        "relative.md",
        str(output_root / "result.txt"),
        str(output_root / ".." / "private" / "result.md"),
    )
    for path in invalid_paths:
        with pytest.raises(ExtractionDomainError) as raised:
            policy.validate_output_path(path)
        assert raised.value.code is ExtractionErrorCode.OUTPUT_PATH_NOT_ALLOWED


def test_output_rejects_symlink_escape(tmp_path: Path) -> None:
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    private_root = tmp_path / "private"
    input_root.mkdir()
    output_root.mkdir()
    private_root.mkdir()
    link = output_root / "linked"
    try:
        link.symlink_to(private_root, target_is_directory=True)
    except OSError:
        pytest.skip("当前环境不允许创建目录符号链接")

    with pytest.raises(ExtractionDomainError) as raised:
        make_policy(input_root, output_root).validate_output_path(
            str(link / "result.md")
        )

    assert raised.value.code is ExtractionErrorCode.OUTPUT_PATH_NOT_ALLOWED


@pytest.mark.parametrize(
    "url",
    [
        "ftp://files.internal/a.txt",
        "http://user:pass@files.internal/a.txt",
        "http://files.internal/a.txt#fragment",
        "http://other.internal/a.txt",
        "http://files.internal:8080/a.txt",
    ],
)
def test_remote_url_rejects_unsafe_shape(tmp_path: Path, url: str) -> None:
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    input_root.mkdir()
    output_root.mkdir()

    with pytest.raises(ExtractionDomainError) as raised:
        make_policy(input_root, output_root).validate_remote_url(url)

    assert raised.value.code is ExtractionErrorCode.INPUT_URL_NOT_ALLOWED


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "169.254.169.254", "10.30.0.8"],
)
def test_remote_url_rejects_any_address_outside_allowed_cidr(
    tmp_path: Path,
    address: str,
) -> None:
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    input_root.mkdir()
    output_root.mkdir()
    policy = make_policy(
        input_root,
        output_root,
        resolved_addresses=("10.20.0.8", address),
    )

    with pytest.raises(ExtractionDomainError) as raised:
        policy.validate_remote_url("https://files.internal/a.txt")

    assert raised.value.code is ExtractionErrorCode.INPUT_URL_NOT_ALLOWED


def test_remote_url_accepts_allowlisted_host_and_all_addresses(
    tmp_path: Path,
) -> None:
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    input_root.mkdir()
    output_root.mkdir()

    result = make_policy(input_root, output_root).validate_remote_url(
        "https://FILES.internal/a%20b.txt?version=1"
    )

    assert result == "https://files.internal/a%20b.txt?version=1"


def test_remote_url_rejects_loopback_even_if_cidr_was_configured(
    tmp_path: Path,
) -> None:
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    input_root.mkdir()
    output_root.mkdir()
    policy = RequestPolicy(
        input_roots=(input_root,),
        output_roots=(output_root,),
        allowed_http_hosts=("files.internal",),
        allowed_http_cidrs=("127.0.0.0/8",),
        max_input_bytes=1024,
        resolver=lambda _host, _port: ("127.0.0.1",),
    )

    with pytest.raises(ExtractionDomainError) as raised:
        policy.validate_remote_url("http://files.internal/a.txt")

    assert raised.value.code is ExtractionErrorCode.INPUT_URL_NOT_ALLOWED
