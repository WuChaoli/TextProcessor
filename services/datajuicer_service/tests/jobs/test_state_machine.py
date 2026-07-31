import pytest

from datajuicer_service.jobs.state_machine import (
    InvalidTransition,
    JobStatus,
    require_transition,
)


def test_supported_job_lifecycle_is_accepted() -> None:
    require_transition(JobStatus.PENDING, JobStatus.QUEUED)
    require_transition(JobStatus.QUEUED, JobStatus.RUNNING)
    require_transition(JobStatus.RUNNING, JobStatus.SUCCEEDED)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (JobStatus.SUCCEEDED, JobStatus.RUNNING),
        (JobStatus.FAILED, JobStatus.QUEUED),
        (JobStatus.PENDING, JobStatus.SUCCEEDED),
        (JobStatus.QUEUED, JobStatus.SUCCEEDED),
    ],
)
def test_illegal_transition_is_rejected(
    current: JobStatus,
    target: JobStatus,
) -> None:
    with pytest.raises(InvalidTransition):
        require_transition(current, target)
