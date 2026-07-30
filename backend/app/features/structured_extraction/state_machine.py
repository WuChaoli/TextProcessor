from typing import Final

from app.features.structured_extraction.models import ExtractionTaskStatus


class InvalidStateTransition(ValueError):
    pass


ALLOWED_TRANSITIONS: Final[dict[ExtractionTaskStatus, frozenset[ExtractionTaskStatus]]] = {
    ExtractionTaskStatus.PENDING: frozenset(
        {
            ExtractionTaskStatus.QUEUED,
            ExtractionTaskStatus.FAILED,
        }
    ),
    ExtractionTaskStatus.QUEUED: frozenset(
        {
            ExtractionTaskStatus.RUNNING,
            ExtractionTaskStatus.FAILED,
            ExtractionTaskStatus.CANCELLED,
        }
    ),
    ExtractionTaskStatus.RUNNING: frozenset(
        {
            ExtractionTaskStatus.SUCCEEDED,
            ExtractionTaskStatus.FAILED,
            ExtractionTaskStatus.CANCELLED,
        }
    ),
    ExtractionTaskStatus.SUCCEEDED: frozenset(),
    ExtractionTaskStatus.FAILED: frozenset(),
    ExtractionTaskStatus.CANCELLED: frozenset(),
}


def assert_transition(
    current: ExtractionTaskStatus,
    target: ExtractionTaskStatus,
) -> None:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidStateTransition(f"非法任务状态转换: {current} -> {target}")

