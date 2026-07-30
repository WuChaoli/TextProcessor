import logging
import uuid
from typing import Protocol

from app.features.structured_extraction.errors import (
    ExtractionDomainError,
    ExtractionErrorCode,
)
from app.features.structured_extraction.models import (
    ExtractionTask,
    ExtractionTaskStatus,
    get_datetime_utc,
)
from app.features.structured_extraction.repository import ExtractionTaskRepository
from app.features.structured_extraction.request_policy import RequestPolicy
from app.features.structured_extraction.schemas import ExtractionTaskCreate

logger = logging.getLogger(__name__)


class ExtractionTaskDispatcher(Protocol):
    def enqueue_submit(self, task_id: uuid.UUID) -> None: ...


class ExtractionTaskService:
    def __init__(
        self,
        repository: ExtractionTaskRepository,
        policy: RequestPolicy,
        dispatcher: ExtractionTaskDispatcher,
    ) -> None:
        self._repository = repository
        self._policy = policy
        self._dispatcher = dispatcher

    def create_task(
        self,
        caller_id: uuid.UUID,
        request: ExtractionTaskCreate,
    ) -> ExtractionTask:
        validated = self._policy.validate_request(request)
        with self._repository.idempotency_lock(
            caller_id,
            validated.session_id,
            validated.file_id,
        ):
            task, created = self._repository.create_or_get(
                caller_id=caller_id,
                session_id=validated.session_id,
                file_id=validated.file_id,
                file_storage_path=validated.file_storage_path,
                file_oss_url=validated.file_oss_url,
                selected_input_type=validated.selected_input_type,
                target_path=validated.target_path,
            )
            if not created:
                if (
                    task.status is ExtractionTaskStatus.FAILED
                    and task.error_code == ExtractionErrorCode.QUEUE_SUBMISSION_FAILED
                ):
                    raise self._queue_submission_error()
                return task

            task = self._repository.transition(
                task.id,
                expected=ExtractionTaskStatus.PENDING,
                target=ExtractionTaskStatus.QUEUED,
                queued_at=get_datetime_utc(),
            )
            try:
                self._dispatcher.enqueue_submit(task.id)
            except Exception:
                logger.warning(
                    "structured extraction task queue submission failed",
                    extra={
                        "task_id": str(task.id),
                        "caller_id": str(caller_id),
                        "error_code": ExtractionErrorCode.QUEUE_SUBMISSION_FAILED,
                    },
                )
                self._repository.transition(
                    task.id,
                    expected=ExtractionTaskStatus.QUEUED,
                    target=ExtractionTaskStatus.FAILED,
                    error_code=ExtractionErrorCode.QUEUE_SUBMISSION_FAILED,
                    error_message="任务提交失败",
                    finished_at=get_datetime_utc(),
                )
                raise self._queue_submission_error() from None
            return task

    def get_task(
        self,
        caller_id: uuid.UUID,
        task_id: uuid.UUID,
    ) -> ExtractionTask | None:
        return self._repository.get_for_caller(task_id, caller_id)

    @staticmethod
    def _queue_submission_error() -> ExtractionDomainError:
        return ExtractionDomainError(
            ExtractionErrorCode.QUEUE_SUBMISSION_FAILED,
            "任务提交失败，请稍后重试",
            http_status=503,
        )
