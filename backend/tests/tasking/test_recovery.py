from uuid import UUID, uuid4

from app.tasking.recovery import recover_due_tasks


class FakeRepository:
    def __init__(self, task_ids: list[UUID]) -> None:
        self.task_ids = task_ids
        self.marked: list[UUID] = []
        self.rollbacks = 0

    def due_task_ids(self) -> list[UUID]:
        return self.task_ids

    def mark_dispatched(self, task_id: UUID) -> bool:
        self.marked.append(task_id)
        return True

    def rollback(self) -> None:
        self.rollbacks += 1


class FakeDispatcher:
    def __init__(self, failing_id: UUID | None = None) -> None:
        self.failing_id = failing_id
        self.ids: list[UUID] = []

    def enqueue_submit(self, task_id: UUID) -> None:
        if task_id == self.failing_id:
            raise RuntimeError("broker unavailable")
        self.ids.append(task_id)


def test_recover_dispatches_each_due_task_once() -> None:
    first_id, second_id = uuid4(), uuid4()
    repository = FakeRepository([first_id, second_id])
    dispatcher = FakeDispatcher()

    assert recover_due_tasks(repository, dispatcher) == 2
    assert dispatcher.ids == [first_id, second_id]
    assert repository.marked == [first_id, second_id]


def test_recover_continues_after_dispatch_failure() -> None:
    first_id, second_id = uuid4(), uuid4()
    repository = FakeRepository([first_id, second_id])
    dispatcher = FakeDispatcher(failing_id=first_id)

    assert recover_due_tasks(repository, dispatcher) == 1
    assert dispatcher.ids == [second_id]
    assert repository.marked == [second_id]
    assert repository.rollbacks == 1
