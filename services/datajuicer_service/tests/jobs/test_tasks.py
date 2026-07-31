from uuid import uuid4

import pytest
from pydantic import ValidationError

from datajuicer_service.core.celery_app import create_celery_app
from datajuicer_service.jobs.tasks import register_tasks


class RecordingOrchestrator:
    def __init__(self) -> None:
        self.executed = []
        self.recovery_calls = 0

    def execute(self, job_id) -> None:
        self.executed.append(job_id)

    def recover(self) -> int:
        self.recovery_calls += 1
        return 2


def test_registered_tasks_validate_message_and_invoke_orchestrator() -> None:
    app = create_celery_app(
        broker_url="memory://",
        queue="datajuicer.jobs",
        recovery_interval_seconds=30,
    )
    app.conf.task_always_eager = True
    orchestrator = RecordingOrchestrator()
    register_tasks(app, lambda: orchestrator, max_attempts=3)
    job_id = uuid4()

    app.tasks["datajuicer.execute"].apply(
        args=[
            {
                "jobId": str(job_id),
                "taskType": "datajuicer_job",
                "schemaVersion": 1,
            }
        ]
    ).get()
    recovered = app.tasks["datajuicer.recover"].apply().get()

    assert orchestrator.executed == [job_id]
    assert recovered == 2
    assert orchestrator.recovery_calls == 1


def test_execute_task_rejects_unknown_message_fields() -> None:
    app = create_celery_app(
        broker_url="memory://",
        queue="datajuicer.jobs",
        recovery_interval_seconds=30,
    )
    app.conf.task_always_eager = True
    orchestrator = RecordingOrchestrator()
    register_tasks(app, lambda: orchestrator, max_attempts=3)

    result = app.tasks["datajuicer.execute"].apply(
        args=[
            {
                "jobId": str(uuid4()),
                "taskType": "datajuicer_job",
                "schemaVersion": 1,
                "unexpected": True,
            }
        ]
    )

    with pytest.raises(ValidationError):
        result.get()
    assert orchestrator.executed == []
