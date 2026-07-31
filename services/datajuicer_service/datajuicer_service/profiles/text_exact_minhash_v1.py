import hashlib
import json
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid5

from datajuicer_service.profiles.exact import group_exact
from datajuicer_service.profiles.io import (
    InputLimits,
    load_and_validate_output_jsonl,
    load_input_jsonl,
)
from datajuicer_service.profiles.minhash import MinHashConfig, cluster_minhash
from datajuicer_service.profiles.models import (
    ClusterDecision,
    ClusterMethod,
    InputSample,
)

PROFILE_NAME = "text_exact_minhash_v1"
CLUSTER_NAMESPACE = UUID("f02873a0-e739-4e4b-a0ce-285f47b87923")
ProgressCallback = Callable[[str, int, int | None], None]
PreparedCallback = Callable[[Path, str, str, int], None]


class OutputConflictError(FileExistsError):
    pass


@dataclass(frozen=True, slots=True)
class ProfileResult:
    output_path: str
    output_sha256: str
    input_sha256: str
    input_count: int
    cluster_count: int


def _cluster_id(request_id: str, member_uids: tuple[int, ...]) -> str:
    members = ",".join(str(uid) for uid in member_uids)
    return str(uuid5(CLUSTER_NAMESPACE, f"{request_id}\0{members}"))


def _representative_uid(
    member_uids: tuple[int, ...],
    samples_by_uid: dict[int, InputSample],
) -> int:
    return min(
        member_uids,
        key=lambda uid: (-len(samples_by_uid[uid].text.strip()), uid),
    )


def _decisions_for_group(
    member_uids: tuple[int, ...],
    method: ClusterMethod,
    request_id: str,
    samples_by_uid: dict[int, InputSample],
) -> list[ClusterDecision]:
    cluster_id = _cluster_id(request_id, member_uids)
    representative_uid = _representative_uid(member_uids, samples_by_uid)
    return [
        ClusterDecision(
            uid=uid,
            cluster_id=cluster_id,
            representative=uid == representative_uid,
            method=method,
        )
        for uid in member_uids
    ]


def execute_samples(
    samples: Sequence[InputSample],
    *,
    request_id: str,
    progress: ProgressCallback | None = None,
) -> tuple[ClusterDecision, ...]:
    report = progress or (lambda _phase, _processed, _total: None)
    samples_by_uid = {sample.uid: sample for sample in samples}
    report("exact_grouping", 0, len(samples))
    exact_result = group_exact(samples)
    exact_members_by_representative = {
        group.representative_uid: group.member_uids for group in exact_result.groups
    }
    representative_samples = [
        samples_by_uid[uid]
        for uid in (
            *exact_members_by_representative,
            *exact_result.independent_uids,
        )
    ]

    report("minhash_computing", 0, len(representative_samples))
    minhash_clusters = cluster_minhash(representative_samples, MinHashConfig.v1())
    report("minhash_clustering", len(representative_samples), len(representative_samples))
    grouped_uids: set[int] = set()
    consumed_exact_representatives: set[int] = set()
    decisions: list[ClusterDecision] = []

    report("expanding_clusters", 0, len(samples))
    for minhash_cluster in minhash_clusters:
        expanded: set[int] = set()
        contains_exact_group = False
        for representative_uid in minhash_cluster.member_uids:
            exact_members = exact_members_by_representative.get(representative_uid)
            if exact_members is None:
                expanded.add(representative_uid)
                continue
            contains_exact_group = True
            consumed_exact_representatives.add(representative_uid)
            expanded.update(exact_members)
        member_uids = tuple(sorted(expanded))
        method: ClusterMethod = "exact_minhash" if contains_exact_group else "minhash"
        decisions.extend(
            _decisions_for_group(
                member_uids,
                method,
                request_id,
                samples_by_uid,
            )
        )
        grouped_uids.update(member_uids)

    for representative_uid, member_uids in exact_members_by_representative.items():
        if representative_uid in consumed_exact_representatives:
            continue
        decisions.extend(
            _decisions_for_group(
                member_uids,
                "exact",
                request_id,
                samples_by_uid,
            )
        )
        grouped_uids.update(member_uids)

    for uid in sorted(samples_by_uid.keys() - grouped_uids):
        decisions.append(
            ClusterDecision(
                uid=uid,
                cluster_id=None,
                representative=True,
                method=None,
            )
        )
    decisions.sort(key=lambda decision: decision.uid)
    return tuple(decisions)


def _write_decisions(path: Path, decisions: Sequence[ClusterDecision]) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        for decision in decisions:
            record = {
                "uid": decision.uid,
                "clusterId": decision.cluster_id,
                "representative": decision.representative,
                "method": decision.method,
            }
            stream.write(
                json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            )
        stream.flush()
        os.fsync(stream.fileno())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def publish_prepared_output(
    staging_path: Path,
    output_path: Path,
    expected_sha256: str,
) -> None:
    if sha256_file(staging_path) != expected_sha256:
        raise OutputConflictError("PREPARED_OUTPUT_DIGEST_MISMATCH")
    try:
        os.link(staging_path, output_path)
    except FileExistsError as error:
        raise OutputConflictError("OUTPUT_CONFLICT") from error
    staging_path.unlink()


class TextExactMinhashV1:
    name = PROFILE_NAME
    version = "1"

    def __init__(self, limits: InputLimits) -> None:
        self._limits = limits

    def execute(
        self,
        input_path: Path,
        output_path: Path,
        *,
        request_id: str,
        progress: ProgressCallback | None = None,
        prepared: PreparedCallback | None = None,
    ) -> ProfileResult:
        report = progress or (lambda _phase, _processed, _total: None)
        record_prepared = prepared or (
            lambda _path, _output_sha256, _input_sha256, _input_count: None
        )
        if output_path.exists():
            raise OutputConflictError("OUTPUT_CONFLICT")

        report("validating_input", 0, None)
        input_sha256 = sha256_file(input_path)
        samples = load_input_jsonl(input_path, self._limits)
        decisions = execute_samples(
            samples,
            request_id=request_id,
            progress=report,
        )
        report("writing_result", 0, len(samples))

        request_digest = hashlib.sha256(request_id.encode()).hexdigest()[:16]
        temporary_path = output_path.with_name(
            f".{output_path.name}.{request_digest}.part"
        )
        if temporary_path.exists():
            raise OutputConflictError("TEMP_OUTPUT_CONFLICT")

        published = False
        prepared_persisted = False
        try:
            _write_decisions(temporary_path, decisions)
            load_and_validate_output_jsonl(
                temporary_path,
                {sample.uid for sample in samples},
            )
            output_sha256 = sha256_file(temporary_path)
            record_prepared(
                temporary_path,
                output_sha256,
                input_sha256,
                len(samples),
            )
            prepared_persisted = True
            publish_prepared_output(
                temporary_path,
                output_path,
                output_sha256,
            )
            published = True
        finally:
            if temporary_path.exists() and (published or not prepared_persisted):
                temporary_path.unlink()

        if not published:
            raise RuntimeError("OUTPUT_PUBLISH_FAILED")
        report("completed", len(samples), len(samples))
        return ProfileResult(
            output_path=str(output_path),
            output_sha256=output_sha256,
            input_sha256=input_sha256,
            input_count=len(samples),
            cluster_count=len(
                {
                    decision.cluster_id
                    for decision in decisions
                    if decision.cluster_id is not None
                }
            ),
        )
