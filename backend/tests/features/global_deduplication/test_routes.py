import json
import uuid
from collections.abc import Generator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, delete

from app.api.deps import get_current_user, get_db
from app.core.db import engine
from app.features.global_deduplication.request_policy import (
    GlobalDeduplicationRequestPolicy,
)
from app.features.global_deduplication.routes import (
    get_global_deduplication_dispatcher,
    get_global_deduplication_policy,
    task_to_public,
)
from app.features.global_deduplication.state_machine import (
    GlobalDeduplicationTaskStatus,
)
from app.features.global_deduplication.task_models import GlobalDeduplicationTask
from app.main import app
from app.models import User


@dataclass
class Dispatcher:
    fail: bool = False
    task_ids: list[uuid.UUID] = field(default_factory=list)

    def enqueue_submit(self, task_id: uuid.UUID) -> None:
        if self.fail:
            raise RuntimeError("broker unavailable")
        self.task_ids.append(task_id)


@pytest.fixture
def api_context(
    tmp_path: Path,
) -> Generator[tuple[TestClient, uuid.UUID, Dispatcher, Path, Path]]:
    caller_id = uuid.uuid7()
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    input_root.mkdir()
    output_root.mkdir()
    manifest = input_root / "manifest.json"
    manifest.write_text("[]", encoding="utf-8")
    dispatcher = Dispatcher()
    policy = GlobalDeduplicationRequestPolicy(
        input_roots=(input_root,),
        output_roots=(output_root,),
        allowed_http_hosts=(),
        allowed_http_cidrs=(),
    )
    with Session(engine) as session:
        session.add(
            User(
                id=caller_id,
                email=f"global-api-{caller_id}@example.com",
                hashed_password="not-used",
            )
        )
        session.commit()

    def session_dependency() -> Generator[Session]:
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = session_dependency
    app.dependency_overrides[get_current_user] = lambda: User(
        id=caller_id,
        email=f"global-api-{caller_id}@example.com",
        hashed_password="not-used",
    )
    app.dependency_overrides[get_global_deduplication_policy] = lambda: policy
    app.dependency_overrides[get_global_deduplication_dispatcher] = (
        lambda: dispatcher
    )
    try:
        with TestClient(app) as client:
            yield client, caller_id, dispatcher, manifest, output_root
    finally:
        app.dependency_overrides.clear()
        with Session(engine) as session:
            session.exec(
                delete(GlobalDeduplicationTask).where(
                    GlobalDeduplicationTask.caller_id == caller_id
                )
            )
            session.exec(delete(User).where(User.id == caller_id))
            session.commit()


def test_post_is_idempotent_and_get_has_no_result_body(
    api_context: tuple[TestClient, uuid.UUID, Dispatcher, Path, Path],
) -> None:
    client, _caller_id, dispatcher, manifest, output_root = api_context
    payload = {
        "sessionId": "session-1",
        "inputJsonPath": str(manifest),
        "targetPath": str(output_root / "result.json"),
    }

    first = client.post("/api/v1/global-deduplication/tasks", json=payload)
    repeated = client.post("/api/v1/global-deduplication/tasks", json=payload)

    assert first.status_code == 202
    assert repeated.status_code == 202
    assert first.json() == repeated.json()
    assert len(dispatcher.task_ids) == 1
    task_id = first.json()["taskId"]

    response = client.get(f"/api/v1/global-deduplication/tasks/{task_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "queued"
    assert body["progress"]["phase"] == "validating_input"
    assert body["result"] is None
    assert body["error"] is None
    assert json.dumps(body).find("same content") == -1


def test_post_conflict_and_queue_failure_have_stable_errors(
    api_context: tuple[TestClient, uuid.UUID, Dispatcher, Path, Path],
) -> None:
    client, _caller_id, dispatcher, manifest, output_root = api_context
    payload = {
        "sessionId": "session-conflict",
        "inputJsonPath": str(manifest),
        "targetPath": str(output_root / "first.json"),
    }
    assert client.post(
        "/api/v1/global-deduplication/tasks",
        json=payload,
    ).status_code == 202
    conflict = client.post(
        "/api/v1/global-deduplication/tasks",
        json={**payload, "targetPath": str(output_root / "second.json")},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"

    dispatcher.fail = True
    unavailable = client.post(
        "/api/v1/global-deduplication/tasks",
        json={
            **payload,
            "sessionId": "session-unavailable",
            "targetPath": str(output_root / "unavailable.json"),
        },
    )
    assert unavailable.status_code == 503
    assert unavailable.json()["detail"]["code"] == "QUEUE_SUBMISSION_FAILED"


def test_get_hides_tasks_from_other_callers(
    api_context: tuple[TestClient, uuid.UUID, Dispatcher, Path, Path],
) -> None:
    client, _caller_id, _dispatcher, manifest, output_root = api_context
    created = client.post(
        "/api/v1/global-deduplication/tasks",
        json={
            "sessionId": "session-private",
            "inputJsonPath": str(manifest),
            "targetPath": str(output_root / "private.json"),
        },
    )
    task_id = created.json()["taskId"]
    app.dependency_overrides[get_current_user] = lambda: User(
        id=uuid.uuid7(),
        email="other@example.com",
        hashed_password="not-used",
    )

    hidden = client.get(f"/api/v1/global-deduplication/tasks/{task_id}")
    missing = client.get(
        f"/api/v1/global-deduplication/tasks/{uuid.uuid7()}"
    )

    assert hidden.status_code == 404
    assert missing.status_code == 404
    assert hidden.json() == missing.json()


def test_running_task_does_not_expose_transient_worker_error() -> None:
    task = GlobalDeduplicationTask(
        caller_id=uuid.uuid7(),
        session_id="session-transient",
        request_fingerprint="a" * 64,
        input_json_path="/input.json",
        target_path="/result.json",
        status=GlobalDeduplicationTaskStatus.RUNNING,
        processing_phase="submitting",
        error_code="PROCESSOR_UNAVAILABLE",
        error_message="处理器当前不可用",
    )

    public = task_to_public(task)

    assert public.status is GlobalDeduplicationTaskStatus.RUNNING
    assert public.error is None
