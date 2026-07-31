import hashlib
import json
from pathlib import Path

import pytest

from datajuicer_service.profiles.io import InputLimits
from datajuicer_service.profiles.models import InputSample
from datajuicer_service.profiles.registry import UnknownProfileError, get_profile
from datajuicer_service.profiles.text_exact_minhash_v1 import (
    OutputConflictError,
    TextExactMinhashV1,
    execute_samples,
)

REQUEST_ID = "0198f000-0000-7000-8000-000000000001"
LIMITS = InputLimits(max_records=100, max_bytes=1024 * 1024, max_text_chars=1_000_000)
LONG_FULL_TEXT = "这是一个包含稳定主体、数据、分析过程与结论的中文测试文档。" * 30
LONG_NEAR_DUPLICATE = LONG_FULL_TEXT[:-1]
UNRELATED_TEXT = "数据库迁移说明与前述内容完全无关。" * 30


def test_profile_expands_exact_group_into_minhash_cluster() -> None:
    samples = [
        InputSample(uid=0, text=LONG_FULL_TEXT),
        InputSample(uid=1, text=f"  {LONG_FULL_TEXT}\n"),
        InputSample(uid=2, text=LONG_NEAR_DUPLICATE),
        InputSample(uid=3, text=UNRELATED_TEXT),
    ]

    decisions = execute_samples(samples, request_id=REQUEST_ID)

    grouped = [item for item in decisions if item.cluster_id is not None]
    assert {item.uid for item in grouped} == {0, 1, 2}
    assert {item.method for item in grouped} == {"exact_minhash"}
    assert sum(item.representative for item in grouped) == 1
    assert next(item.uid for item in grouped if item.representative) == 0
    assert decisions[-1].cluster_id is None
    assert decisions[-1].representative is True
    assert decisions[-1].method is None


def test_profile_distinguishes_exact_and_minhash_only_groups() -> None:
    exact_only = execute_samples(
        [
            InputSample(uid=3, text=" same "),
            InputSample(uid=1, text="same"),
            InputSample(uid=8, text="different"),
        ],
        request_id=REQUEST_ID,
    )
    minhash_only = execute_samples(
        [
            InputSample(uid=0, text=LONG_FULL_TEXT),
            InputSample(uid=2, text=LONG_NEAR_DUPLICATE),
        ],
        request_id=REQUEST_ID,
    )

    assert [item.method for item in exact_only] == ["exact", "exact", None]
    assert [item.method for item in minhash_only] == ["minhash", "minhash"]


def test_cluster_id_is_deterministic_and_request_scoped() -> None:
    samples = [
        InputSample(uid=4, text="duplicate"),
        InputSample(uid=2, text=" duplicate "),
    ]

    first = execute_samples(samples, request_id=REQUEST_ID)
    repeated = execute_samples(list(reversed(samples)), request_id=REQUEST_ID)
    another_request = execute_samples(samples, request_id=f"{REQUEST_ID}-other")

    assert first == repeated
    assert first[0].cluster_id == first[1].cluster_id
    assert first[0].cluster_id != another_request[0].cluster_id


def test_profile_writes_validated_jsonl_without_text(tmp_path: Path) -> None:
    input_path = tmp_path / "input.jsonl"
    output_path = tmp_path / "output.jsonl"
    input_path.write_text(
        "\n".join(
            [
                json.dumps({"uid": 1, "text": "same"}),
                json.dumps({"uid": 0, "text": " same "}),
                json.dumps({"uid": 2, "text": "unique"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    phases: list[str] = []

    result = TextExactMinhashV1(LIMITS).execute(
        input_path,
        output_path,
        request_id=REQUEST_ID,
        progress=lambda phase, _processed, _total: phases.append(phase),
    )

    output_bytes = output_path.read_bytes()
    records = [json.loads(line) for line in output_bytes.decode().splitlines()]
    assert records == [
        {
            "uid": 0,
            "clusterId": records[0]["clusterId"],
            "representative": True,
            "method": "exact",
        },
        {
            "uid": 1,
            "clusterId": records[0]["clusterId"],
            "representative": False,
            "method": "exact",
        },
        {
            "uid": 2,
            "clusterId": None,
            "representative": True,
            "method": None,
        },
    ]
    assert result.output_sha256 == hashlib.sha256(output_bytes).hexdigest()
    assert result.input_count == 3
    assert result.cluster_count == 1
    assert phases == [
        "validating_input",
        "exact_grouping",
        "minhash_computing",
        "minhash_clustering",
        "expanding_clusters",
        "writing_result",
        "completed",
    ]
    assert not list(tmp_path.glob("*.part"))


def test_profile_never_overwrites_existing_output(tmp_path: Path) -> None:
    input_path = tmp_path / "input.jsonl"
    output_path = tmp_path / "output.jsonl"
    input_path.write_text('{"uid":0,"text":"only"}\n', encoding="utf-8")
    output_path.write_text("sentinel", encoding="utf-8")

    with pytest.raises(OutputConflictError, match="OUTPUT_CONFLICT"):
        TextExactMinhashV1(LIMITS).execute(
            input_path,
            output_path,
            request_id=REQUEST_ID,
        )

    assert output_path.read_text(encoding="utf-8") == "sentinel"


def test_profile_records_prepared_digest_before_atomic_publish(tmp_path: Path) -> None:
    input_path = tmp_path / "input.jsonl"
    output_path = tmp_path / "output.jsonl"
    input_path.write_text('{"uid":0,"text":"only"}\n', encoding="utf-8")
    observed: list[tuple[Path, str, bool, bool]] = []

    def record_prepared(
        staging_path: Path,
        output_sha256: str,
        _input_sha256: str,
        _input_count: int,
    ) -> None:
        observed.append(
            (
                staging_path,
                output_sha256,
                staging_path.exists(),
                output_path.exists(),
            )
        )

    result = TextExactMinhashV1(LIMITS).execute(
        input_path,
        output_path,
        request_id=REQUEST_ID,
        prepared=record_prepared,
    )

    assert observed == [
        (
            observed[0][0],
            result.output_sha256,
            True,
            False,
        )
    ]
    assert result.input_sha256 == hashlib.sha256(input_path.read_bytes()).hexdigest()


def test_registry_only_exposes_v1_profile() -> None:
    assert isinstance(get_profile("text_exact_minhash_v1", LIMITS), TextExactMinhashV1)
    with pytest.raises(UnknownProfileError, match="UNKNOWN_PROFILE"):
        get_profile("arbitrary_recipe", LIMITS)
