from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class InputSample:
    uid: int
    text: str


@dataclass(frozen=True, slots=True)
class ExactGroup:
    member_uids: tuple[int, ...]
    representative_uid: int


@dataclass(frozen=True, slots=True)
class ExactGroupingResult:
    groups: tuple[ExactGroup, ...]
    independent_uids: tuple[int, ...]


ClusterMethod = Literal["exact", "minhash", "exact_minhash"]


@dataclass(frozen=True, slots=True)
class ClusterDecision:
    uid: int
    cluster_id: str | None
    representative: bool
    method: ClusterMethod | None
