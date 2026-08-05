import uuid
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.api.deps import get_current_user, get_db
from app.features.structured_extraction.request_policy import RequestPolicy
from app.features.structured_extraction.routes import (
    get_extraction_dispatcher,
    get_request_policy,
)
from app.main import app
from app.models import User


class RecordingDispatcher:
    def __init__(self, failure: Exception | None = None) -> None:
        self.task_ids: list[uuid.UUID] = []
        self.failure = failure

    def enqueue_submit(self, task_id: uuid.UUID) -> None:
        self.task_ids.append(task_id)
        if self.failure:
            raise self.failure


@pytest.fixture
def api_context(
    tmp_path: Path,
) -> Generator[tuple[TestClient, Session, User, RecordingDispatcher, dict[str, str]],]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    session = Session(engine)
    user = User(
        id=uuid.uuid4(),
        email="caller@example.com",
        hashed_password="not-used",
    )
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    input_root.mkdir()
    output_root.mkdir()
    source = input_root / "sample.txt"
    source.write_text("hello", encoding="utf-8")
    policy = RequestPolicy(
        input_roots=(input_root,),
        output_roots=(output_root,),
        allowed_http_hosts=(),
        allowed_http_cidrs=(),
        max_input_bytes=1024,
    )
    dispatcher = RecordingDispatcher()
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_request_policy] = lambda: policy
    app.dependency_overrides[get_extraction_dispatcher] = lambda: dispatcher
    payload = {
        "sessionId": "session-001",
        "fileId": "11",
        "fileStoragePath": str(source),
        "fileOssUrl": None,
        "targetPath": str(output_root / "sample.md"),
    }
    with TestClient(app) as client:
        yield client, session, user, dispatcher, payload
    app.dependency_overrides.clear()
    session.close()


def test_create_returns_202_camel_case_and_is_idempotent(
    api_context: tuple[
        TestClient,
        Session,
        User,
        RecordingDispatcher,
        dict[str, str],
    ],
) -> None:
    client, _session, _user, dispatcher, payload = api_context

    first = client.post("/api/v1/structured-extraction/tasks", json=payload)
    second = client.post("/api/v1/structured-extraction/tasks", json=payload)

    assert first.status_code == 202
    assert first.json() == second.json()
    assert set(first.json()) == {
        "taskId",
        "sessionId",
        "fileId",
        "status",
        "createdAt",
    }
    assert first.json()["status"] == "queued"
    assert len(dispatcher.task_ids) == 1


def test_create_rejects_changed_request_for_same_idempotency_key(
    api_context: tuple[
        TestClient,
        Session,
        User,
        RecordingDispatcher,
        dict[str, str],
    ],
) -> None:
    client, _session, _user, _dispatcher, payload = api_context
    assert (
        client.post("/api/v1/structured-extraction/tasks", json=payload).status_code
        == 202
    )
    changed = {
        **payload,
        "targetPath": str(Path(payload["targetPath"]).with_name("other.md")),
    }

    response = client.post("/api/v1/structured-extraction/tasks", json=changed)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_create_maps_queue_failure_without_leaking_detail(
    api_context: tuple[
        TestClient,
        Session,
        User,
        RecordingDispatcher,
        dict[str, str],
    ],
) -> None:
    client, _session, _user, _dispatcher, payload = api_context
    app.dependency_overrides[get_extraction_dispatcher] = lambda: RecordingDispatcher(
        RuntimeError("redis://user:secret@broker")
    )

    response = client.post("/api/v1/structured-extraction/tasks", json=payload)

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "QUEUE_SUBMISSION_FAILED"
    assert "secret" not in response.text


def test_create_validation_error_is_422(
    api_context: tuple[
        TestClient,
        Session,
        User,
        RecordingDispatcher,
        dict[str, str],
    ],
) -> None:
    client, _session, _user, _dispatcher, payload = api_context
    payload["sessionId"] = ""

    response = client.post("/api/v1/structured-extraction/tasks", json=payload)

    assert response.status_code == 422


def test_get_returns_snapshot_and_other_caller_gets_same_404(
    api_context: tuple[
        TestClient,
        Session,
        User,
        RecordingDispatcher,
        dict[str, str],
    ],
) -> None:
    client, _session, user, _dispatcher, payload = api_context
    created = client.post("/api/v1/structured-extraction/tasks", json=payload).json()

    response = client.get(f"/api/v1/structured-extraction/tasks/{created['taskId']}")

    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    assert response.json()["result"] is None
    assert response.json()["error"] is None

    app.dependency_overrides[get_current_user] = lambda: User(
        id=uuid.uuid4(),
        email="other@example.com",
        hashed_password="not-used",
    )
    hidden = client.get(f"/api/v1/structured-extraction/tasks/{created['taskId']}")
    missing = client.get(f"/api/v1/structured-extraction/tasks/{uuid.uuid4()}")
    assert hidden.status_code == missing.status_code == 404
    assert hidden.json() == missing.json()
    assert hidden.json() == {
        "detail": {
            "code": "TASK_NOT_FOUND",
            "message": "任务不存在",
        }
    }
    assert user.id != app.dependency_overrides[get_current_user]().id
