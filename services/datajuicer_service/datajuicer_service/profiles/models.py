from dataclasses import dataclass


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
