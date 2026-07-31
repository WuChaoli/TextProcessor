import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.features.global_deduplication.errors import (
    GlobalDeduplicationErrorCode,
    GlobalDeduplicationProcessingError,
)

ClusterMethod = Literal["exact", "minhash", "exact_minhash"]


@dataclass(frozen=True, slots=True)
class MappingDocument:
    uid: int
    file_id: str
    file_storage_path: str


@dataclass(frozen=True, slots=True)
class ClusterDecision:
    uid: int
    cluster_id: str | None
    representative: bool
    method: ClusterMethod | None


@dataclass(frozen=True, slots=True)
class BusinessResult:
    file_id: str
    file_storage_path: str
    group_id: str | None
    keep: bool

    def to_public_dict(self) -> dict[str, str | bool | None]:
        return {
            "fileId": self.file_id,
            "fileStoragePath": self.file_storage_path,
            "groupId": self.group_id,
            "keep": self.keep,
        }


def _invalid_output() -> GlobalDeduplicationProcessingError:
    return GlobalDeduplicationProcessingError(
        GlobalDeduplicationErrorCode.INVALID_PROCESSOR_OUTPUT,
        "处理器输出不符合契约",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
    except OSError:
        raise _invalid_output() from None
    return digest.hexdigest()


def validate_processor_output(
    path: Path,
    *,
    expected_uids: set[int],
    expected_sha256: str,
) -> tuple[ClusterDecision, ...]:
    if _sha256(path) != expected_sha256:
        raise _invalid_output()
    decisions: list[ClusterDecision] = []
    seen_uids: set[int] = set()
    groups: dict[str, list[ClusterDecision]] = {}
    try:
        with path.open(encoding="utf-8") as source:
            for line in source:
                record = json.loads(line)
                if not isinstance(record, dict) or set(record) != {
                    "uid",
                    "clusterId",
                    "representative",
                    "method",
                }:
                    raise _invalid_output()
                uid = record["uid"]
                cluster_id = record["clusterId"]
                representative = record["representative"]
                method = record["method"]
                if (
                    isinstance(uid, bool)
                    or not isinstance(uid, int)
                    or uid < 0
                    or uid in seen_uids
                    or (
                        cluster_id is not None
                        and (
                            not isinstance(cluster_id, str)
                            or not cluster_id
                        )
                    )
                    or not isinstance(representative, bool)
                    or method not in {None, "exact", "minhash", "exact_minhash"}
                ):
                    raise _invalid_output()
                if cluster_id is None:
                    if not representative or method is not None:
                        raise _invalid_output()
                elif method is None:
                    raise _invalid_output()
                decision = ClusterDecision(
                    uid=uid,
                    cluster_id=cluster_id,
                    representative=representative,
                    method=method,
                )
                decisions.append(decision)
                seen_uids.add(uid)
                if cluster_id is not None:
                    groups.setdefault(cluster_id, []).append(decision)
    except GlobalDeduplicationProcessingError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise _invalid_output() from None

    if seen_uids != expected_uids:
        raise _invalid_output()
    for members in groups.values():
        if (
            len(members) < 2
            or sum(member.representative for member in members) != 1
            or len({member.method for member in members}) != 1
        ):
            raise _invalid_output()
    decisions.sort(key=lambda item: item.uid)
    return tuple(decisions)


def map_business_result(
    task_id: uuid.UUID,
    mapping: tuple[MappingDocument, ...],
    decisions: tuple[ClusterDecision, ...],
) -> tuple[BusinessResult, ...]:
    mapping_by_uid = {document.uid: document for document in mapping}
    if (
        len(mapping_by_uid) != len(mapping)
        or set(mapping_by_uid) != {decision.uid for decision in decisions}
    ):
        raise _invalid_output()
    members_by_cluster: dict[str, list[int]] = {}
    for decision in decisions:
        if decision.cluster_id is not None:
            members_by_cluster.setdefault(decision.cluster_id, []).append(
                decision.uid
            )
    business_group_ids = {
        cluster_id: str(
            uuid.uuid5(
                task_id,
                ",".join(str(uid) for uid in sorted(member_uids)),
            )
        )
        for cluster_id, member_uids in members_by_cluster.items()
    }
    return tuple(
        BusinessResult(
            file_id=mapping_by_uid[decision.uid].file_id,
            file_storage_path=mapping_by_uid[
                decision.uid
            ].file_storage_path,
            group_id=(
                None
                if decision.cluster_id is None
                else business_group_ids[decision.cluster_id]
            ),
            keep=decision.representative,
        )
        for decision in sorted(decisions, key=lambda item: item.uid)
    )
