import logging

from app.tasking.contracts import RecoverableTaskRepository, TaskDispatcher

logger = logging.getLogger(__name__)


def recover_due_tasks(
    repository: RecoverableTaskRepository,
    dispatcher: TaskDispatcher,
) -> int:
    recovered = 0
    for task_id in repository.due_task_ids():
        try:
            dispatcher.enqueue_submit(task_id)
        except Exception:
            repository.rollback()
            logger.warning(
                "task recovery dispatch failed",
                extra={"task_id": str(task_id)},
            )
            continue
        try:
            marked = repository.mark_dispatched(task_id)
        except Exception:
            repository.rollback()
            logger.warning(
                "task recovery marker write failed",
                extra={"task_id": str(task_id)},
            )
            continue
        if marked:
            recovered += 1
    return recovered
