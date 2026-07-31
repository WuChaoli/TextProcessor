from dataclasses import asdict, dataclass
from typing import Protocol
from uuid import UUID

from celery import Celery  # type: ignore[import-untyped]


@dataclass(frozen=True, slots=True)
class ExecutionMessage:
    job_id: UUID
    task_type: str = "datajuicer_job"
    schema_version: int = 1

    def as_payload(self) -> dict[str, str | int]:
        payload = asdict(self)
        return {
            "jobId": str(payload["job_id"]),
            "taskType": str(payload["task_type"]),
            "schemaVersion": int(payload["schema_version"]),
        }


class JobDispatcher(Protocol):
    def enqueue(self, message: ExecutionMessage) -> None: ...


class CeleryJobDispatcher:
    def __init__(self, celery_app: Celery, *, queue: str) -> None:
        self._celery_app = celery_app
        self._queue = queue

    def enqueue(self, message: ExecutionMessage) -> None:
        self._celery_app.send_task(
            "datajuicer.execute",
            args=[message.as_payload()],
            queue=self._queue,
        )
