from enum import StrEnum
from typing import Final


class GlobalDeduplicationTaskStatus(StrEnum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class InvalidGlobalDeduplicationStateTransition(ValueError):
    pass


ALLOWED_TRANSITIONS: Final[
    dict[
        GlobalDeduplicationTaskStatus,
        frozenset[GlobalDeduplicationTaskStatus],
    ]
] = {
    GlobalDeduplicationTaskStatus.PENDING: frozenset(
        {
            GlobalDeduplicationTaskStatus.QUEUED,
            GlobalDeduplicationTaskStatus.FAILED,
        }
    ),
    GlobalDeduplicationTaskStatus.QUEUED: frozenset(
        {
            GlobalDeduplicationTaskStatus.RUNNING,
            GlobalDeduplicationTaskStatus.FAILED,
            GlobalDeduplicationTaskStatus.CANCELLED,
        }
    ),
    GlobalDeduplicationTaskStatus.RUNNING: frozenset(
        {
            GlobalDeduplicationTaskStatus.SUCCEEDED,
            GlobalDeduplicationTaskStatus.FAILED,
            GlobalDeduplicationTaskStatus.CANCELLED,
        }
    ),
    GlobalDeduplicationTaskStatus.SUCCEEDED: frozenset(),
    GlobalDeduplicationTaskStatus.FAILED: frozenset(),
    GlobalDeduplicationTaskStatus.CANCELLED: frozenset(),
}


def assert_transition(
    current: GlobalDeduplicationTaskStatus,
    target: GlobalDeduplicationTaskStatus,
) -> None:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidGlobalDeduplicationStateTransition(
            f"非法全局去重任务状态转换: {current} -> {target}"
        )
