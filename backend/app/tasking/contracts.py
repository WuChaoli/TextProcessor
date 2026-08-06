from collections.abc import Iterable
from typing import Protocol
from uuid import UUID


class RecoverableTaskRepository(Protocol):
    def due_task_ids(self) -> Iterable[UUID]: ...

    def mark_dispatched(self, task_id: UUID) -> bool: ...

    def rollback(self) -> None: ...


class TaskDispatcher(Protocol):
    def enqueue_submit(self, task_id: UUID) -> None: ...
