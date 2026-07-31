import logging
import uuid
from datetime import UTC, datetime
from typing import Protocol

from app.features.global_deduplication.api_errors import (
    GlobalDeduplicationApiErrorCode,
    GlobalDeduplicationDomainError,
)
from app.features.global_deduplication.repository import (
    GlobalDeduplicationTaskRepository,
)
from app.features.global_deduplication.request_policy import (
    GlobalDeduplicationRequestPolicy,
)
from app.features.global_deduplication.schemas import (
    GlobalDeduplicationTaskCreate,
)
from app.features.global_deduplication.state_machine import (
    GlobalDeduplicationTaskStatus,
)
from app.features.global_deduplication.task_models import GlobalDeduplicationTask

logger = logging.getLogger(__name__)


class GlobalDeduplicationTaskDispatcher(Protocol):
    def enqueue_submit(self, task_id: uuid.UUID) -> None: ...


class GlobalDeduplicationTaskService:
    def __init__(
        self,
        repository: GlobalDeduplicationTaskRepository,
        policy: GlobalDeduplicationRequestPolicy,
        dispatcher: GlobalDeduplicationTaskDispatcher,
    ) -> None:
        self._repository = repository
        self._policy = policy
        self._dispatcher = dispatcher

    def create_task(
        self,
        caller_id: uuid.UUID,
        request: GlobalDeduplicationTaskCreate,
    ) -> GlobalDeduplicationTask:
        validated = self._policy.validate_request(request)
        with self._repository.idempotency_lock(
            caller_id,
            validated.session_id,
        ):
            task, created = self._repository.create_or_get(
                caller_id=caller_id,
                session_id=validated.session_id,
                input_json_path=validated.input_json_path,
                target_path=validated.target_path,
            )
            if not created:
                if (
                    task.status is GlobalDeduplicationTaskStatus.FAILED
                    and task.error_code
                    == GlobalDeduplicationApiErrorCode.QUEUE_SUBMISSION_FAILED
                ):
                    raise self._queue_error()
                return task
            now = datetime.now(UTC)
            task = self._repository.transition(
                task.id,
                expected=GlobalDeduplicationTaskStatus.PENDING,
                target=GlobalDeduplicationTaskStatus.QUEUED,
                queued_at=now,
                processing_phase="validating_input",
                updated_at=now,
            )
            try:
                self._dispatcher.enqueue_submit(task.id)
            except Exception:
                logger.warning(
                    "global deduplication queue submission failed",
                    extra={
                        "task_id": str(task.id),
                        "caller_id": str(caller_id),
                        "error_code": (
                            GlobalDeduplicationApiErrorCode.QUEUE_SUBMISSION_FAILED
                        ),
                    },
                )
                finished = datetime.now(UTC)
                self._repository.transition(
                    task.id,
                    expected=GlobalDeduplicationTaskStatus.QUEUED,
                    target=GlobalDeduplicationTaskStatus.FAILED,
                    error_code=(
                        GlobalDeduplicationApiErrorCode.QUEUE_SUBMISSION_FAILED
                    ),
                    error_message="任务提交失败",
                    finished_at=finished,
                    processing_phase=None,
                    updated_at=finished,
                )
                raise self._queue_error() from None
            try:
                marked = self._repository.mark_dispatched(
                    task.id,
                    now=datetime.now(UTC),
                )
            except Exception:
                self._repository.rollback()
                marked = False
            if not marked:
                logger.info(
                    "global deduplication dispatch marker not updated",
                    extra={
                        "task_id": str(task.id),
                        "caller_id": str(caller_id),
                    },
                )
            return task

    def get_task(
        self,
        caller_id: uuid.UUID,
        task_id: uuid.UUID,
    ) -> GlobalDeduplicationTask | None:
        return self._repository.get_for_caller(task_id, caller_id)

    @staticmethod
    def _queue_error() -> GlobalDeduplicationDomainError:
        return GlobalDeduplicationDomainError(
            GlobalDeduplicationApiErrorCode.QUEUE_SUBMISSION_FAILED,
            "任务提交失败，请稍后重试",
            http_status=503,
        )
