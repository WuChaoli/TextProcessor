from pathlib import Path

import pytest

from app.features.global_deduplication.api_errors import (
    GlobalDeduplicationDomainError,
)
from app.features.global_deduplication.request_policy import (
    GlobalDeduplicationRequestPolicy,
)
from app.features.global_deduplication.schemas import (
    GlobalDeduplicationTaskCreate,
)


@pytest.fixture(autouse=True)
def db() -> None:
    """请求策略单测不依赖 PostgreSQL。"""


def request(input_path: str, target_path: str) -> GlobalDeduplicationTaskCreate:
    return GlobalDeduplicationTaskCreate(
        sessionId="session-1",
        inputJsonPath=input_path,
        targetPath=target_path,
    )


def test_local_paths_are_normalized_under_configured_roots(
    tmp_path: Path,
) -> None:
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    input_root.mkdir()
    output_root.mkdir()
    manifest = input_root / "manifest.json"
    manifest.write_text("[]", encoding="utf-8")
    policy = GlobalDeduplicationRequestPolicy(
        input_roots=(input_root,),
        output_roots=(output_root,),
        allowed_http_hosts=(),
        allowed_http_cidrs=(),
    )

    validated = policy.validate_request(
        request(str(manifest), str(output_root / "result.json"))
    )

    assert validated.input_json_path == str(manifest.resolve())
    assert validated.target_path == str((output_root / "result.json").resolve())


def test_paths_outside_configured_roots_use_runtime_access(tmp_path: Path) -> None:
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    outside = tmp_path / "outside"
    input_root.mkdir()
    output_root.mkdir()
    outside.mkdir()
    manifest = outside / "manifest.json"
    manifest.write_text("[]", encoding="utf-8")
    policy = GlobalDeduplicationRequestPolicy(
        input_roots=(input_root,),
        output_roots=(output_root,),
        allowed_http_hosts=(),
        allowed_http_cidrs=(),
    )

    validated = policy.validate_request(
        request(str(manifest), str(outside / "result.json"))
    )

    assert validated.input_json_path == str(manifest.resolve())
    assert validated.target_path == str((outside / "result.json").resolve())


def test_http_manifest_requires_host_and_cidr_allowlist(tmp_path: Path) -> None:
    output_root = tmp_path / "output"
    output_root.mkdir()
    policy = GlobalDeduplicationRequestPolicy(
        input_roots=(),
        output_roots=(output_root,),
        allowed_http_hosts=("files.internal",),
        allowed_http_cidrs=("10.20.0.0/16",),
        resolver=lambda _host, _port: ("10.20.0.8",),
    )

    validated = policy.validate_request(
        request(
            "https://files.internal/manifest.json",
            str(output_root / "result.json"),
        )
    )
    assert validated.input_json_path == "https://files.internal/manifest.json"

    with pytest.raises(GlobalDeduplicationDomainError) as error:
        policy.validate_request(
            request(
                "https://evil.internal/manifest.json",
                str(output_root / "result.json"),
            )
        )
    assert error.value.code == "INPUT_URL_NOT_ALLOWED"


def test_s3_input_and_output_require_bucket_allowlist() -> None:
    policy = GlobalDeduplicationRequestPolicy(
        input_roots=(),
        output_roots=(Path("/unused"),),
        allowed_http_hosts=(),
        allowed_http_cidrs=(),
        allowed_s3_buckets=("approved",),
    )

    validated = policy.validate_request(
        request(
            "s3://approved/manifests/input.json",
            "s3://approved/results/output.json",
        )
    )

    assert validated.input_json_path == "s3://approved/manifests/input.json"
    assert validated.target_path == "s3://approved/results/output.json"

    with pytest.raises(GlobalDeduplicationDomainError) as error:
        policy.validate_input("s3://other/manifests/input.json")
    assert error.value.code == "INPUT_PATH_NOT_ALLOWED"
