import pytest

from app.features.global_deduplication.state_machine import (
    GlobalDeduplicationTaskStatus,
    InvalidGlobalDeduplicationStateTransition,
    assert_transition,
)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("pending", "queued"),
        ("pending", "failed"),
        ("queued", "running"),
        ("queued", "cancelled"),
        ("running", "succeeded"),
        ("running", "failed"),
    ],
)
def test_state_machine_accepts_legal_transitions(
    current: str,
    target: str,
) -> None:
    assert_transition(
        GlobalDeduplicationTaskStatus(current),
        GlobalDeduplicationTaskStatus(target),
    )


def test_state_machine_rejects_terminal_and_skipped_transitions() -> None:
    with pytest.raises(InvalidGlobalDeduplicationStateTransition):
        assert_transition(
            GlobalDeduplicationTaskStatus.QUEUED,
            GlobalDeduplicationTaskStatus.SUCCEEDED,
        )
    with pytest.raises(InvalidGlobalDeduplicationStateTransition):
        assert_transition(
            GlobalDeduplicationTaskStatus.SUCCEEDED,
            GlobalDeduplicationTaskStatus.RUNNING,
        )
