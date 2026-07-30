import pytest

from app.features.structured_extraction.models import ExtractionTaskStatus
from app.features.structured_extraction.state_machine import (
    InvalidStateTransition,
    assert_transition,
)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (ExtractionTaskStatus.PENDING, ExtractionTaskStatus.QUEUED),
        (ExtractionTaskStatus.PENDING, ExtractionTaskStatus.FAILED),
        (ExtractionTaskStatus.QUEUED, ExtractionTaskStatus.RUNNING),
        (ExtractionTaskStatus.QUEUED, ExtractionTaskStatus.FAILED),
        (ExtractionTaskStatus.QUEUED, ExtractionTaskStatus.CANCELLED),
        (ExtractionTaskStatus.RUNNING, ExtractionTaskStatus.SUCCEEDED),
        (ExtractionTaskStatus.RUNNING, ExtractionTaskStatus.FAILED),
        (ExtractionTaskStatus.RUNNING, ExtractionTaskStatus.CANCELLED),
    ],
)
def test_allowed_transitions(
    current: ExtractionTaskStatus,
    target: ExtractionTaskStatus,
) -> None:
    assert_transition(current, target)


@pytest.mark.parametrize(
    "terminal",
    [
        ExtractionTaskStatus.SUCCEEDED,
        ExtractionTaskStatus.FAILED,
        ExtractionTaskStatus.CANCELLED,
    ],
)
def test_terminal_status_cannot_transition(terminal: ExtractionTaskStatus) -> None:
    with pytest.raises(InvalidStateTransition):
        assert_transition(terminal, ExtractionTaskStatus.RUNNING)


def test_running_cannot_return_to_queued() -> None:
    with pytest.raises(InvalidStateTransition):
        assert_transition(
            ExtractionTaskStatus.RUNNING,
            ExtractionTaskStatus.QUEUED,
        )
