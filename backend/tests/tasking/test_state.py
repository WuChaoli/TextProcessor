import pytest

from app.tasking.state import IllegalTaskTransition, TaskStatus, ensure_transition


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (TaskStatus.PENDING, TaskStatus.QUEUED),
        (TaskStatus.PENDING, TaskStatus.FAILED),
        (TaskStatus.PENDING, TaskStatus.CANCELLED),
        (TaskStatus.QUEUED, TaskStatus.RUNNING),
        (TaskStatus.QUEUED, TaskStatus.FAILED),
        (TaskStatus.QUEUED, TaskStatus.CANCELLED),
        (TaskStatus.RUNNING, TaskStatus.SUCCEEDED),
        (TaskStatus.RUNNING, TaskStatus.FAILED),
        (TaskStatus.RUNNING, TaskStatus.CANCELLED),
    ],
)
def test_supported_transition(current: TaskStatus, target: TaskStatus) -> None:
    ensure_transition(current, target)


@pytest.mark.parametrize("status", list(TaskStatus))
def test_terminal_state_cannot_transition(status: TaskStatus) -> None:
    if status not in {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
        return
    with pytest.raises(IllegalTaskTransition):
        ensure_transition(status, TaskStatus.RUNNING)
