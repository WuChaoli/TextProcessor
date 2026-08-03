import pytest

from app.features.markdown_cleaning.state_machine import (
    InvalidMarkdownCleaningStateTransition,
    MarkdownCleaningTaskStatus,
    assert_transition,
)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (MarkdownCleaningTaskStatus.PENDING, MarkdownCleaningTaskStatus.QUEUED),
        (MarkdownCleaningTaskStatus.QUEUED, MarkdownCleaningTaskStatus.RUNNING),
        (MarkdownCleaningTaskStatus.RUNNING, MarkdownCleaningTaskStatus.SUCCEEDED),
        (MarkdownCleaningTaskStatus.RUNNING, MarkdownCleaningTaskStatus.FAILED),
        (MarkdownCleaningTaskStatus.RUNNING, MarkdownCleaningTaskStatus.CANCELLED),
    ],
)
def test_legal_transitions(
    current: MarkdownCleaningTaskStatus, target: MarkdownCleaningTaskStatus
) -> None:
    assert_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (MarkdownCleaningTaskStatus.PENDING, MarkdownCleaningTaskStatus.RUNNING),
        (MarkdownCleaningTaskStatus.QUEUED, MarkdownCleaningTaskStatus.SUCCEEDED),
        (MarkdownCleaningTaskStatus.SUCCEEDED, MarkdownCleaningTaskStatus.RUNNING),
        (MarkdownCleaningTaskStatus.FAILED, MarkdownCleaningTaskStatus.QUEUED),
        (MarkdownCleaningTaskStatus.CANCELLED, MarkdownCleaningTaskStatus.QUEUED),
    ],
)
def test_illegal_transitions_are_rejected(
    current: MarkdownCleaningTaskStatus, target: MarkdownCleaningTaskStatus
) -> None:
    with pytest.raises(InvalidMarkdownCleaningStateTransition):
        assert_transition(current, target)
