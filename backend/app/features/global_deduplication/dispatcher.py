import uuid

from app.features.global_deduplication.celery_tasks import (
    submit_global_deduplication_task,
)
from app.features.global_deduplication.messages import GlobalDeduplicationMessage


class CeleryGlobalDeduplicationTaskDispatcher:
    def enqueue_submit(self, task_id: uuid.UUID) -> None:
        payload = GlobalDeduplicationMessage(
            taskId=task_id,
            taskType="global_deduplication",
            schemaVersion=1,
        ).as_payload()
        submit_global_deduplication_task.apply_async(  # pyright: ignore[reportFunctionMemberAccess]
            kwargs=payload
        )
