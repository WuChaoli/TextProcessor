from collections.abc import Sequence
from hashlib import sha256

from datajuicer_service.profiles.models import (
    ExactGroup,
    ExactGroupingResult,
    InputSample,
)


def _digest_text(text: str) -> bytes:
    return sha256(text.encode("utf-8")).digest()


def group_exact(samples: Sequence[InputSample]) -> ExactGroupingResult:
    buckets: dict[bytes, dict[str, list[int]]] = {}
    for sample in samples:
        normalized = sample.text.strip()
        bucket = buckets.setdefault(_digest_text(normalized), {})
        bucket.setdefault(normalized, []).append(sample.uid)

    groups: list[ExactGroup] = []
    independent_uids: list[int] = []
    for bucket in buckets.values():
        for member_uids in bucket.values():
            ordered_uids = tuple(sorted(member_uids))
            if len(ordered_uids) == 1:
                independent_uids.append(ordered_uids[0])
                continue
            groups.append(
                ExactGroup(
                    member_uids=ordered_uids,
                    representative_uid=ordered_uids[0],
                )
            )

    groups.sort(key=lambda group: group.member_uids)
    independent_uids.sort()
    return ExactGroupingResult(
        groups=tuple(groups),
        independent_uids=tuple(independent_uids),
    )
