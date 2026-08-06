import uuid
from typing import Protocol

from app.features.text_classification.models import ClassificationTask, utc_now
from app.features.text_classification.repository import ClassificationTaskRepository
from app.features.text_classification.schemas import ClassificationTaskCreate
from app.tasking.state import TaskStatus


class Dispatcher(Protocol):
    def enqueue(self, task_id: uuid.UUID) -> None: ...


class ClassificationTaskService:
    def __init__(self, repository: ClassificationTaskRepository, dispatcher: Dispatcher) -> None:
        self._repository = repository
        self._dispatcher = dispatcher

    def create_task(self, caller_id: uuid.UUID, request: ClassificationTaskCreate) -> ClassificationTask:
        task, created = self._repository.create_or_get(caller_id=caller_id, session_id=request.session_id, file_id=request.file_id, input_uri=request.input_uri)
        if not created:
            return task
        task = self._repository.transition(task.id, expected=TaskStatus.PENDING, target=TaskStatus.QUEUED, queued_at=utc_now())
        try:
            self._dispatcher.enqueue(task.id)
        except Exception:
            self._repository.transition(task.id, expected=TaskStatus.QUEUED, target=TaskStatus.FAILED, error_code="QUEUE_SUBMISSION_FAILED", error_message="任务提交失败", finished_at=utc_now())
            raise
        self._repository.mark_dispatched(task.id)
        return task

    def get_task(self, caller_id: uuid.UUID, task_id: uuid.UUID) -> ClassificationTask | None:
        return self._repository.get_for_caller(task_id, caller_id)
