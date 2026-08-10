import uuid
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from app.features.global_deduplication.api_errors import (
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
from app.features.global_deduplication.service import (
    GlobalDeduplicationTaskService,
)
from app.features.global_deduplication.state_machine import (
    GlobalDeduplicationTaskStatus,
)
from tests.features.global_deduplication.test_submit_orchestration import (
    build_session,
)


@dataclass
class Dispatcher:
    fail: bool = False
    task_ids: list[uuid.UUID] = field(default_factory=list)

    def enqueue_submit(self, task_id: uuid.UUID) -> None:
        if self.fail:
            raise RuntimeError("broker unavailable")
        self.task_ids.append(task_id)


def build_service(
    tmp_path: Path,
    *,
    dispatcher: Dispatcher,
) -> tuple[GlobalDeduplicationTaskService, Path, Path]:
    batch = tmp_path / "batch"
    (batch / "original").mkdir(parents=True)
    (batch / "duplicate").mkdir()
    alternative = tmp_path / "alternative"
    (alternative / "original").mkdir(parents=True)
    (alternative / "duplicate").mkdir()
    session = build_session()
    return (
        GlobalDeduplicationTaskService(
            GlobalDeduplicationTaskRepository(session),
            GlobalDeduplicationRequestPolicy(),
            dispatcher,
        ),
        batch,
        alternative,
    )


def create_request(batch: Path) -> GlobalDeduplicationTaskCreate:
    return GlobalDeduplicationTaskCreate(
        sessionId="session-1",
        inputPath=str(batch),
    )


def test_create_is_idempotent_and_dispatches_only_once(tmp_path: Path) -> None:
    dispatcher = Dispatcher()
    service, batch, _alternative = build_service(
        tmp_path,
        dispatcher=dispatcher,
    )
    caller_id = uuid.uuid7()
    request = create_request(batch)

    first = service.create_task(caller_id, request)
    repeated = service.create_task(caller_id, request)

    assert first.id == repeated.id
    assert first.status is GlobalDeduplicationTaskStatus.QUEUED
    assert dispatcher.task_ids == [first.id]


def test_same_session_with_different_path_conflicts(tmp_path: Path) -> None:
    service, batch, alternative = build_service(
        tmp_path,
        dispatcher=Dispatcher(),
    )
    caller_id = uuid.uuid7()
    service.create_task(
        caller_id,
        create_request(batch),
    )

    with pytest.raises(GlobalDeduplicationDomainError) as error:
        service.create_task(
            caller_id,
            create_request(alternative),
        )

    assert error.value.code == "IDEMPOTENCY_CONFLICT"


def test_queue_failure_is_persisted_and_returned_as_503(
    tmp_path: Path,
) -> None:
    service, batch, _alternative = build_service(
        tmp_path,
        dispatcher=Dispatcher(fail=True),
    )

    with pytest.raises(GlobalDeduplicationDomainError) as error:
        service.create_task(
            uuid.uuid7(),
            create_request(batch),
        )

    assert error.value.code == "QUEUE_SUBMISSION_FAILED"
    assert error.value.http_status == 503
