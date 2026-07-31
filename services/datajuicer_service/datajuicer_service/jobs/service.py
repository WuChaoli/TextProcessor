from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from datajuicer_service.jobs.dispatcher import ExecutionMessage, JobDispatcher
from datajuicer_service.jobs.models import DataJuicerJob
from datajuicer_service.jobs.repository import (
    CreateJobResult,
    JobCreate,
    JobError,
)


class JobRepositoryProtocol(Protocol):
    def create_or_get(
        self,
        request: JobCreate,
        *,
        now: datetime,
    ) -> CreateJobResult: ...

    def get(self, job_id: UUID) -> DataJuicerJob | None: ...

    def mark_queued(self, job_id: UUID, *, now: datetime) -> None: ...

    def mark_failed(
        self,
        job_id: UUID,
        lease_token: UUID | None,
        error: JobError,
        *,
        now: datetime,
    ) -> None: ...


RepositoryFactory = Callable[
    [],
    AbstractContextManager[JobRepositoryProtocol],
]


@dataclass(frozen=True, slots=True)
class CreateJobCommand:
    request_id: str
    profile: str
    input_path: str
    output_path: str


class QueueSubmissionError(RuntimeError):
    pass


class JobService:
    def __init__(
        self,
        *,
        repository_factory: RepositoryFactory,
        dispatcher: JobDispatcher,
        max_attempts: int,
        job_timeout_seconds: int,
        now: Callable[[], datetime],
    ) -> None:
        self._repository_factory = repository_factory
        self._dispatcher = dispatcher
        self._max_attempts = max_attempts
        self._job_timeout_seconds = job_timeout_seconds
        self._now = now

    def create_job(self, command: CreateJobCommand) -> DataJuicerJob:
        now = self._now()
        request = JobCreate(
            request_id=command.request_id,
            profile=command.profile,
            input_path=command.input_path,
            output_path=command.output_path,
            max_attempts=self._max_attempts,
            processing_deadline=now + timedelta(seconds=self._job_timeout_seconds),
        )
        with self._repository_factory() as repository:
            created = repository.create_or_get(request, now=now)
            if not created.created:
                return created.job
            message = ExecutionMessage(job_id=created.job.job_id)
            try:
                self._dispatcher.enqueue(message)
            except Exception as error:
                repository.mark_failed(
                    created.job.job_id,
                    lease_token=None,
                    error=JobError(
                        code="QUEUE_SUBMISSION_FAILED",
                        message="任务入队失败",
                    ),
                    now=self._now(),
                )
                raise QueueSubmissionError("QUEUE_SUBMISSION_FAILED") from error
            repository.mark_queued(created.job.job_id, now=self._now())
            queued_job = repository.get(created.job.job_id)
            if queued_job is None:
                raise RuntimeError("JOB_DISAPPEARED_AFTER_QUEUE")
            return queued_job

    def get_job(self, job_id: UUID) -> DataJuicerJob | None:
        with self._repository_factory() as repository:
            return repository.get(job_id)
