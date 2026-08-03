from pathlib import Path
from unittest.mock import Mock

import pytest

from app.features.markdown_cleaning.api_errors import MarkdownCleaningDomainError
from app.features.markdown_cleaning.request_policy import MarkdownCleaningRequestPolicy
from app.features.markdown_cleaning.schemas import MarkdownCleaningTaskCreate


def build_request(
    *,
    storage_path: str | None = None,
    oss_url: str | None = None,
    target_path: str,
) -> MarkdownCleaningTaskCreate:
    return MarkdownCleaningTaskCreate(
        sessionId="session-1",
        fileId="file-1",
        fileStoragePath=storage_path,
        fileOssUrl=oss_url,
        targetPath=target_path,
    )


def build_policy(
    *,
    input_roots: tuple[Path, ...],
    output_roots: tuple[Path, ...],
    resolver: Mock | None = None,
) -> MarkdownCleaningRequestPolicy:
    return MarkdownCleaningRequestPolicy(
        input_roots=input_roots,
        output_roots=output_roots,
        allowed_http_hosts=("files.internal",),
        allowed_http_cidrs=("10.0.0.0/8",),
        resolver=resolver or (lambda _host, _port: ("10.20.30.40",)),
    )


def test_local_markdown_input_and_target_are_normalized_under_roots(
    tmp_path: Path,
) -> None:
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    input_root.mkdir()
    output_root.mkdir()
    source = input_root / "source.Md"
    source.write_text("text", encoding="utf-8")
    target = output_root / "result.MARKDOWN"

    validated = build_policy(
        input_roots=(input_root,), output_roots=(output_root,)
    ).validate_request(
        build_request(storage_path=str(source), target_path=str(target))
    )

    assert validated.session_id == "session-1"
    assert validated.file_id == "file-1"
    assert validated.file_storage_path == str(source.resolve())
    assert validated.file_oss_url is None
    assert validated.selected_input_type == "local"
    assert validated.target_path == str(target.resolve())


def test_local_input_outside_allowlist_is_rejected(tmp_path: Path) -> None:
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    outside = tmp_path / "outside.md"
    input_root.mkdir()
    output_root.mkdir()
    outside.write_text("text", encoding="utf-8")

    with pytest.raises(MarkdownCleaningDomainError) as error:
        build_policy(
            input_roots=(input_root,), output_roots=(output_root,)
        ).validate_request(
            build_request(
                storage_path=str(outside), target_path=str(output_root / "result.md")
            )
        )

    assert error.value.code == "INPUT_PATH_NOT_ALLOWED"


def test_symlinked_input_escaping_allowlist_is_rejected(tmp_path: Path) -> None:
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    outside = tmp_path / "outside"
    input_root.mkdir()
    output_root.mkdir()
    outside.mkdir()
    source = outside / "source.md"
    source.write_text("text", encoding="utf-8")
    linked = input_root / "linked.md"
    try:
        linked.symlink_to(source)
    except OSError as exc:
        pytest.skip(f"creating a symlink is unavailable: {exc}")

    with pytest.raises(MarkdownCleaningDomainError) as error:
        build_policy(
            input_roots=(input_root,), output_roots=(output_root,)
        ).validate_request(
            build_request(
                storage_path=str(linked), target_path=str(output_root / "result.md")
            )
        )

    assert error.value.code == "INPUT_PATH_NOT_ALLOWED"


@pytest.mark.parametrize(
    ("url", "resolver_result"),
    [
        ("https://files.internal/source.md", ("10.20.30.40",)),
        ("http://files.internal:80/source.md", ("10.20.30.40",)),
    ],
)
def test_controlled_http_input_is_accepted(
    tmp_path: Path,
    url: str,
    resolver_result: tuple[str, ...],
) -> None:
    output_root = tmp_path / "output"
    output_root.mkdir()

    validated = build_policy(
        input_roots=(),
        output_roots=(output_root,),
        resolver=Mock(return_value=resolver_result),
    ).validate_request(
        build_request(oss_url=url, target_path=str(output_root / "result.md"))
    )

    assert validated.file_storage_path is None
    assert validated.file_oss_url == url
    assert validated.selected_input_type == "remote"


@pytest.mark.parametrize(
    ("url", "resolver_result"),
    [
        ("https://evil.internal/source.md", ("10.20.30.40",)),
        ("https://files.internal:8443/source.md", ("10.20.30.40",)),
        ("https://user:secret@files.internal/source.md", ("10.20.30.40",)),
        ("https://files.internal/source.md", ("10.20.30.40", "127.0.0.1")),
        ("https://files.internal/source.md", ("192.168.1.4",)),
    ],
)
def test_http_input_failing_security_policy_is_rejected(
    tmp_path: Path,
    url: str,
    resolver_result: tuple[str, ...],
) -> None:
    output_root = tmp_path / "output"
    output_root.mkdir()

    with pytest.raises(MarkdownCleaningDomainError) as error:
        build_policy(
            input_roots=(),
            output_roots=(output_root,),
            resolver=Mock(return_value=resolver_result),
        ).validate_request(
            build_request(oss_url=url, target_path=str(output_root / "result.md"))
        )

    assert error.value.code == "INPUT_URL_NOT_ALLOWED"


def test_remote_url_with_signed_query_is_rejected_by_policy(tmp_path: Path) -> None:
    output_root = tmp_path / "output"
    output_root.mkdir()

    with pytest.raises(MarkdownCleaningDomainError) as error:
        build_policy(input_roots=(), output_roots=(output_root,)).validate_remote_url(
            "https://files.internal/source.md?X-Amz-Signature=signed-token"
        )

    assert error.value.code == "INPUT_URL_NOT_ALLOWED"


def test_remote_url_with_fragment_is_rejected_by_request_contract() -> None:
    with pytest.raises(ValueError, match="Markdown 文件路径"):
        build_request(
            oss_url="https://files.internal/source.md#part",
            target_path="C:/output/result.md",
        )


def test_local_input_is_selected_without_resolving_remote_url(tmp_path: Path) -> None:
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    input_root.mkdir()
    output_root.mkdir()
    source = input_root / "source.md"
    source.write_text("text", encoding="utf-8")
    resolver = Mock(side_effect=AssertionError("remote must not be resolved"))

    validated = build_policy(
        input_roots=(input_root,), output_roots=(output_root,), resolver=resolver
    ).validate_request(
        build_request(
            storage_path=str(source),
            oss_url="https://files.internal/source.md",
            target_path=str(output_root / "result.md"),
        )
    )

    assert validated.file_storage_path == str(source.resolve())
    assert validated.file_oss_url is None
    assert validated.selected_input_type == "local"
    resolver.assert_not_called()


def test_remote_input_normalizes_scheme_and_host(tmp_path: Path) -> None:
    output_root = tmp_path / "output"
    output_root.mkdir()

    validated = build_policy(input_roots=(), output_roots=(output_root,)).validate_request(
        build_request(
            oss_url="HTTPS://FILES.INTERNAL./source.md",
            target_path=str(output_root / "result.md"),
        )
    )

    assert validated.file_oss_url == "https://files.internal/source.md"
    assert validated.selected_input_type == "remote"


def test_existing_target_is_accepted_by_api_policy(tmp_path: Path) -> None:
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    input_root.mkdir()
    output_root.mkdir()
    source = input_root / "source.md"
    target = output_root / "result.md"
    source.write_text("text", encoding="utf-8")
    target.write_text("already present", encoding="utf-8")

    validated = build_policy(
        input_roots=(input_root,), output_roots=(output_root,)
    ).validate_request(
        build_request(storage_path=str(source), target_path=str(target))
    )

    assert validated.target_path == str(target.resolve())


def test_target_through_symlink_escaping_output_root_is_rejected(
    tmp_path: Path,
) -> None:
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    outside = tmp_path / "outside"
    input_root.mkdir()
    output_root.mkdir()
    outside.mkdir()
    source = input_root / "source.md"
    source.write_text("text", encoding="utf-8")
    linked_output = output_root / "linked-output"
    try:
        linked_output.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"creating an output symlink is unavailable: {exc}")

    with pytest.raises(MarkdownCleaningDomainError) as error:
        build_policy(
            input_roots=(input_root,), output_roots=(output_root,)
        ).validate_request(
            build_request(
                storage_path=str(source),
                target_path=str(linked_output / "result.md"),
            )
        )

    assert error.value.code == "OUTPUT_PATH_NOT_ALLOWED"


def test_input_and_target_resolving_to_same_file_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "shared"
    root.mkdir()
    source = root / "source.md"
    source.write_text("text", encoding="utf-8")

    with pytest.raises(MarkdownCleaningDomainError) as error:
        build_policy(input_roots=(root,), output_roots=(root,)).validate_request(
            build_request(storage_path=str(source), target_path=str(source))
        )

    assert error.value.code == "OUTPUT_PATH_NOT_ALLOWED"


def test_relative_paths_are_rejected_by_policy(tmp_path: Path) -> None:
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    input_root.mkdir()
    output_root.mkdir()

    with pytest.raises(MarkdownCleaningDomainError) as input_error:
        build_policy(
            input_roots=(input_root,), output_roots=(output_root,)
        ).validate_request(
            build_request(
                storage_path="relative.md", target_path=str(output_root / "result.md")
            )
        )

    assert input_error.value.code == "INPUT_PATH_NOT_ALLOWED"

    source = input_root / "source.md"
    source.write_text("text", encoding="utf-8")
    with pytest.raises(MarkdownCleaningDomainError) as output_error:
        build_policy(
            input_roots=(input_root,), output_roots=(output_root,)
        ).validate_request(build_request(storage_path=str(source), target_path="relative.md"))

    assert output_error.value.code == "OUTPUT_PATH_NOT_ALLOWED"


@pytest.mark.parametrize(
    ("storage_path", "target_path"),
    [
        ("C:/input/source.txt", "C:/output/result.md"),
        ("C:/input/source.md", "C:/output/result.txt"),
    ],
)
def test_non_markdown_paths_are_rejected_by_request_contract(
    storage_path: str, target_path: str
) -> None:
    with pytest.raises(ValueError):
        build_request(storage_path=storage_path, target_path=target_path)
