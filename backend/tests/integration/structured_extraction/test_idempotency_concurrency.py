import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path

import pytest
from sqlmodel import Session

from app.core.db import engine
from app.features.structured_extraction.models import ExtractionTaskStatus
from app.features.structured_extraction.repository import ExtractionTaskRepository
from app.features.structured_extraction.request_policy import RequestPolicy
from app.features.structured_extraction.schemas import ExtractionTaskCreate
from app.features.structured_extraction.service import ExtractionTaskService
from app.models import User


class RecordingDispatcher:
    def __init__(self) -> None:
        self.task_ids: list[uuid.UUID] = []
        self._lock = threading.Lock()

    def enqueue_submit(self, task_id: uuid.UUID) -> None:
        with self._lock:
            self.task_ids.append(task_id)


class PausingRepository(ExtractionTaskRepository):
    def __init__(
        self,
        session: Session,
        transition_entered: threading.Event,
        release_transition: threading.Event,
    ) -> None:
        super().__init__(session)
        self._transition_entered = transition_entered
        self._release_transition = release_transition

    def transition(
        self,
        task_id: uuid.UUID,
        *,
        expected: ExtractionTaskStatus,
        target: ExtractionTaskStatus,
        **fields: object,
    ):  # type: ignore[no-untyped-def]
        if (
            expected is ExtractionTaskStatus.PENDING
            and target is ExtractionTaskStatus.QUEUED
        ):
            self._transition_entered.set()
            if not self._release_transition.wait(timeout=5):
                raise TimeoutError("test did not release transition")
        return super().transition(
            task_id,
            expected=expected,
            target=target,
            **fields,
        )


def test_same_idempotency_key_waits_for_first_queue_result(tmp_path: Path) -> None:
    assert engine.dialect.name == "postgresql"
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    input_root.mkdir()
    output_root.mkdir()
    source = input_root / "sample.txt"
    source.write_text("hello", encoding="utf-8")
    request = ExtractionTaskCreate(
        sessionId=f"concurrent-{uuid.uuid4()}",
        fileId="file-1",
        fileStoragePath=str(source),
        targetPath=str(output_root / "sample.md"),
    )
    policy = RequestPolicy(
        allowed_http_hosts=(),
        allowed_http_cidrs=(),
        max_input_bytes=1024,
    )
    dispatcher = RecordingDispatcher()
    transition_entered = threading.Event()
    release_transition = threading.Event()
    caller_id = uuid.uuid4()
    with Session(engine) as setup_session:
        setup_session.add(
            User(
                id=caller_id,
                email=f"concurrent-{caller_id}@example.com",
                hashed_password="not-used",
            )
        )
        setup_session.commit()

    def submit(*, pause_before_queue: bool) -> tuple[uuid.UUID, ExtractionTaskStatus]:
        with Session(engine) as session:
            repository = (
                PausingRepository(
                    session,
                    transition_entered,
                    release_transition,
                )
                if pause_before_queue
                else ExtractionTaskRepository(session)
            )
            task = ExtractionTaskService(
                repository,
                policy,
                dispatcher,
            ).create_task(caller_id, request)
            return task.id, task.status

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(submit, pause_before_queue=True)
        assert transition_entered.wait(timeout=5)
        second = executor.submit(submit, pause_before_queue=False)
        with pytest.raises(FutureTimeoutError):
            second.result(timeout=0.5)
        release_transition.set()
        first_result = first.result(timeout=5)
        second_result = second.result(timeout=5)

    assert first_result == second_result
    assert first_result[1] is ExtractionTaskStatus.QUEUED
    assert dispatcher.task_ids == [first_result[0]]
