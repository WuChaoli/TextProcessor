import logging
import uuid

from app.features.markdown_cleaning.api_errors import (
    MarkdownCleaningApiErrorCode,
    MarkdownCleaningDomainError,
)
from app.features.markdown_cleaning.dispatcher import MarkdownCleaningTaskDispatcher
from app.features.markdown_cleaning.repository import (
    MarkdownCleaningTaskRepository,
)
from app.features.markdown_cleaning.request_policy import (
    MarkdownCleaningRequestPolicy,
    ValidatedMarkdownCleaningRequest,
)
from app.features.markdown_cleaning.schemas import MarkdownCleaningTaskCreate
from app.features.markdown_cleaning.state_machine import (
    MarkdownCleaningTaskStatus,
)
from app.features.markdown_cleaning.task_models import (
    MarkdownCleaningTask,
    get_datetime_utc,
)

logger = logging.getLogger(__name__)


class MarkdownCleaningTaskService:
    def __init__(
        self,
        repository: MarkdownCleaningTaskRepository,
        policy: MarkdownCleaningRequestPolicy,
        dispatcher: MarkdownCleaningTaskDispatcher,
    ) -> None:
        self._repository = repository
        self._policy = policy
        self._dispatcher = dispatcher

    def create_task(
        self,
        caller_id: uuid.UUID,
        request: MarkdownCleaningTaskCreate,
    ) -> MarkdownCleaningTask:
        validated: ValidatedMarkdownCleaningRequest = self._policy.validate_request(
            request
        )
        with self._repository.idempotency_lock(
            caller_id=caller_id,
            session_id=validated.session_id,
            file_id=validated.file_id,
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
                    task.status is MarkdownCleaningTaskStatus.FAILED
                    and task.error_code
                    == MarkdownCleaningApiErrorCode.QUEUE_SUBMISSION_FAILED
                ):
                    raise self._queue_error()
                return task

            now = get_datetime_utc()
            task = self._repository.transition(
                task.id,
                expected=MarkdownCleaningTaskStatus.PENDING,
                target=MarkdownCleaningTaskStatus.QUEUED,
                queued_at=now,
                processing_phase="validating_input",
                updated_at=now,
            )
            try:
                self._dispatcher.enqueue_execute(task.id)
            except Exception:
                logger.warning(
                    "markdown cleaning queue submission failed",
                    extra={
                        "task_id": str(task.id),
                        "caller_id": str(caller_id),
                        "error_code": (
                            MarkdownCleaningApiErrorCode.QUEUE_SUBMISSION_FAILED
                        ),
                    },
                )
                failed_at = get_datetime_utc()
                task = self._repository.transition(
                    task.id,
                    expected=MarkdownCleaningTaskStatus.QUEUED,
                    target=MarkdownCleaningTaskStatus.FAILED,
                    error_code=(MarkdownCleaningApiErrorCode.QUEUE_SUBMISSION_FAILED),
                    error_message="任务提交失败",
                    finished_at=failed_at,
                    processing_phase=None,
                    updated_at=failed_at,
                )
                raise self._queue_error() from None

            try:
                marked = self._repository.mark_dispatched(
                    task.id,
                    now=get_datetime_utc(),
                )
            except Exception:
                self._repository.rollback()
                marked = False

            if marked:
                refreshed = self._repository.get(task.id)
                if refreshed is not None:
                    task = refreshed
            return task

    def get_task(
        self,
        caller_id: uuid.UUID,
        task_id: uuid.UUID,
    ) -> MarkdownCleaningTask | None:
        return self._repository.get_for_caller(task_id, caller_id)

    @staticmethod
    def _queue_error() -> MarkdownCleaningDomainError:
        return MarkdownCleaningDomainError(
            MarkdownCleaningApiErrorCode.QUEUE_SUBMISSION_FAILED,
            "任务提交失败，请稍后重试",
            http_status=503,
        )
