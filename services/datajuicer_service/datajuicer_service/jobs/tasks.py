from collections.abc import Callable
from typing import Literal, NoReturn, Protocol
from uuid import UUID

from celery import Celery  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field

from datajuicer_service.jobs.orchestration import RetryableJobError


class TaskOrchestrator(Protocol):
    def execute(self, job_id: UUID) -> None: ...

    def recover(self) -> int: ...


class TaskRequest(Protocol):
    retries: int


class BoundTask(Protocol):
    request: TaskRequest

    def retry(
        self,
        *,
        exc: Exception,
        countdown: int,
        max_retries: int,
    ) -> NoReturn: ...


class ExecutionTaskMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    job_id: UUID = Field(alias="jobId")
    task_type: Literal["datajuicer_job"] = Field(alias="taskType")
    schema_version: Literal[1] = Field(alias="schemaVersion")


def register_tasks(
    celery_app: Celery,
    orchestrator_factory: Callable[[], TaskOrchestrator],
    *,
    max_attempts: int,
) -> None:
    for task_name in ("datajuicer.execute", "datajuicer.recover"):
        if task_name in celery_app.tasks:
            celery_app.tasks.unregister(task_name)

    @celery_app.task(
        bind=True,
        name="datajuicer.execute",
        acks_late=True,
        reject_on_worker_lost=True,
        ignore_result=True,
    )
    def execute_task(task: BoundTask, payload: dict[str, object]) -> None:
        message = ExecutionTaskMessage.model_validate(payload)
        try:
            orchestrator_factory().execute(message.job_id)
        except RetryableJobError as error:
            retries = task.request.retries
            raise task.retry(
                exc=error,
                countdown=min(2**retries, 60),
                max_retries=max_attempts - 1,
            ) from error

    @celery_app.task(
        name="datajuicer.recover",
        ignore_result=True,
    )
    def recover_task() -> int:
        return orchestrator_factory().recover()
