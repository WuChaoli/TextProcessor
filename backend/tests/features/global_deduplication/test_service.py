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
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    input_root.mkdir()
    output_root.mkdir()
    manifest = input_root / "manifest.json"
    manifest.write_text("[]", encoding="utf-8")
    session = build_session()
    return (
        GlobalDeduplicationTaskService(
            GlobalDeduplicationTaskRepository(session),
            GlobalDeduplicationRequestPolicy(
                input_roots=(input_root,),
                output_roots=(output_root,),
                allowed_http_hosts=(),
                allowed_http_cidrs=(),
            ),
            dispatcher,
        ),
        manifest,
        output_root,
    )


def create_request(
    manifest: Path,
    target: Path,
) -> GlobalDeduplicationTaskCreate:
    return GlobalDeduplicationTaskCreate(
        sessionId="session-1",
        inputJsonPath=str(manifest),
        targetPath=str(target),
    )


def test_create_is_idempotent_and_dispatches_only_once(tmp_path: Path) -> None:
    dispatcher = Dispatcher()
    service, manifest, output_root = build_service(
        tmp_path,
        dispatcher=dispatcher,
    )
    caller_id = uuid.uuid7()
    request = create_request(manifest, output_root / "result.json")

    first = service.create_task(caller_id, request)
    repeated = service.create_task(caller_id, request)

    assert first.id == repeated.id
    assert first.status is GlobalDeduplicationTaskStatus.QUEUED
    assert dispatcher.task_ids == [first.id]


def test_same_session_with_different_path_conflicts(tmp_path: Path) -> None:
    service, manifest, output_root = build_service(
        tmp_path,
        dispatcher=Dispatcher(),
    )
    caller_id = uuid.uuid7()
    service.create_task(
        caller_id,
        create_request(manifest, output_root / "first.json"),
    )

    with pytest.raises(GlobalDeduplicationDomainError) as error:
        service.create_task(
            caller_id,
            create_request(manifest, output_root / "second.json"),
        )

    assert error.value.code == "IDEMPOTENCY_CONFLICT"


def test_queue_failure_is_persisted_and_returned_as_503(
    tmp_path: Path,
) -> None:
    service, manifest, output_root = build_service(
        tmp_path,
        dispatcher=Dispatcher(fail=True),
    )

    with pytest.raises(GlobalDeduplicationDomainError) as error:
        service.create_task(
            uuid.uuid7(),
            create_request(manifest, output_root / "result.json"),
        )

    assert error.value.code == "QUEUE_SUBMISSION_FAILED"
    assert error.value.http_status == 503
