from enum import StrEnum
from typing import Final


class MarkdownCleaningTaskStatus(StrEnum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class InvalidMarkdownCleaningStateTransition(ValueError):
    pass


ALLOWED_TRANSITIONS: Final[
    dict[MarkdownCleaningTaskStatus, frozenset[MarkdownCleaningTaskStatus]]
] = {
    MarkdownCleaningTaskStatus.PENDING: frozenset(
        {MarkdownCleaningTaskStatus.QUEUED}
    ),
    MarkdownCleaningTaskStatus.QUEUED: frozenset(
        {MarkdownCleaningTaskStatus.RUNNING}
    ),
    MarkdownCleaningTaskStatus.RUNNING: frozenset(
        {
            MarkdownCleaningTaskStatus.SUCCEEDED,
            MarkdownCleaningTaskStatus.FAILED,
            MarkdownCleaningTaskStatus.CANCELLED,
        }
    ),
    MarkdownCleaningTaskStatus.SUCCEEDED: frozenset(),
    MarkdownCleaningTaskStatus.FAILED: frozenset(),
    MarkdownCleaningTaskStatus.CANCELLED: frozenset(),
}


def assert_transition(
    current: MarkdownCleaningTaskStatus,
    target: MarkdownCleaningTaskStatus,
) -> None:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidMarkdownCleaningStateTransition(
            f"非法 Markdown 清洗任务状态转换: {current} -> {target}"
        )
