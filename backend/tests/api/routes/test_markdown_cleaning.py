import uuid
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.api.deps import get_current_user, get_db
from app.features.markdown_cleaning.api_errors import MarkdownCleaningApiErrorCode
from app.features.markdown_cleaning.request_policy import MarkdownCleaningRequestPolicy
from app.features.markdown_cleaning.routes import (
    get_markdown_cleaning_dispatcher,
    get_markdown_cleaning_request_policy,
)
from app.features.markdown_cleaning.state_machine import MarkdownCleaningTaskStatus
from app.features.markdown_cleaning.task_models import MarkdownCleaningTask
from app.main import app
from app.models import User


class RecordingDispatcher:
    def __init__(self, *, fail: Exception | None = None) -> None:
        self.task_ids: list[uuid.UUID] = []
        self.fail = fail

    def enqueue_execute(self, task_id: uuid.UUID) -> None:
        self.task_ids.append(task_id)
        if self.fail:
            raise self.fail


@pytest.fixture(scope="session", autouse=True)
def db() -> Generator:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    yield


@pytest.fixture
def api_context(
    tmp_path: Path,
) -> Generator:
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
    source = input_root / "sample.md"
    source.write_text("hello", encoding="utf-8")
    policy = MarkdownCleaningRequestPolicy(
        input_roots=(input_root,),
        output_roots=(output_root,),
        allowed_http_hosts=(),
        allowed_http_cidrs=(),
    )
    dispatcher = RecordingDispatcher()
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_markdown_cleaning_request_policy] = lambda: policy
    app.dependency_overrides[get_markdown_cleaning_dispatcher] = lambda: dispatcher
    payload: dict[str, str | None] = {
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


def test_create_returns_202_and_dispatches_once(
    api_context: tuple[
        TestClient, Session, User, RecordingDispatcher, dict[str, str | None]
    ],
) -> None:
    client, _session, _user, dispatcher, payload = api_context

    first = client.post("/api/v1/markdown-cleaning/tasks", json=payload)
    second = client.post("/api/v1/markdown-cleaning/tasks", json=payload)

    assert first.status_code == 202
    assert first.json() == second.json()
    assert set(first.json()) == {"taskId", "sessionId", "fileId", "status"}
    assert first.status_code == second.status_code == 202
    assert first.json()["status"] == "queued"
    assert first.json()["fileId"] == payload["fileId"]
    assert len(dispatcher.task_ids) == 1


def test_create_rejects_changed_request_for_same_idempotency_key(
    api_context: tuple[
        TestClient, Session, User, RecordingDispatcher, dict[str, str | None]
    ],
) -> None:
    client, _session, _user, _dispatcher, payload = api_context
    assert (
        client.post("/api/v1/markdown-cleaning/tasks", json=payload).status_code == 202
    )
    changed = {
        **payload,
        "targetPath": str(Path(str(payload["targetPath"])).with_name("other.md")),
    }

    response = client.post("/api/v1/markdown-cleaning/tasks", json=changed)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == MarkdownCleaningApiErrorCode.IDEMPOTENCY_CONFLICT


def test_create_validation_error_is_422(
    api_context: tuple[
        TestClient, Session, User, RecordingDispatcher, dict[str, str | None]
    ],
) -> None:
    client, _session, _user, _dispatcher, payload = api_context
    payload["sessionId"] = ""
    response = client.post("/api/v1/markdown-cleaning/tasks", json=payload)
    assert response.status_code == 422


def test_create_policy_error_maps_safe_code(
    api_context: tuple[
        TestClient, Session, User, RecordingDispatcher, dict[str, str | None]
    ],
) -> None:
    client, _session, _user, _dispatcher, payload = api_context
    payload["fileStoragePath"] = str(
        Path(str(payload["fileStoragePath"])).with_name("outside.md")
    )
    response = client.post("/api/v1/markdown-cleaning/tasks", json=payload)
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == (
        MarkdownCleaningApiErrorCode.INPUT_PATH_NOT_ALLOWED
    )


def test_create_maps_queue_failure_without_leaking_secret(
    api_context: tuple[
        TestClient, Session, User, RecordingDispatcher, dict[str, str | None]
    ],
) -> None:
    client, _session, _user, _dispatcher, payload = api_context
    app.dependency_overrides[get_markdown_cleaning_dispatcher] = lambda: RecordingDispatcher(
        fail=RuntimeError("redis://user:secret@broker.internal/queue"),
    )

    response = client.post("/api/v1/markdown-cleaning/tasks", json=payload)

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == (
        MarkdownCleaningApiErrorCode.QUEUE_SUBMISSION_FAILED
    )
    assert "secret" not in response.text


def test_get_returns_snapshot_for_pending_and_running_statuses(
    api_context: tuple[
        TestClient, Session, User, RecordingDispatcher, dict[str, str | None]
    ],
) -> None:
    client, session, _user, _dispatcher, payload = api_context
    created = client.post("/api/v1/markdown-cleaning/tasks", json=payload).json()
    task_id = uuid.UUID(created["taskId"])
    queued = client.get(f"/api/v1/markdown-cleaning/tasks/{task_id}")
    assert queued.status_code == 200
    assert queued.json()["status"] == "queued"
    assert queued.json()["result"] is None
    assert queued.json()["error"] is None

    task = session.get(MarkdownCleaningTask, task_id)
    assert task is not None
    task.status = MarkdownCleaningTaskStatus.RUNNING
    task.started_at = datetime.now(UTC)
    task.processing_phase = "cleaning"
    session.add(task)
    session.commit()

    running = client.get(f"/api/v1/markdown-cleaning/tasks/{task_id}")
    assert running.status_code == 200
    assert running.json()["status"] == "running"
    assert running.json()["result"] is None
    assert running.json()["error"] is None


def test_get_maps_success_result_without_internal_fields(
    api_context: tuple[
        TestClient, Session, User, RecordingDispatcher, dict[str, str | None]
    ],
) -> None:
    client, session, _user, _dispatcher, payload = api_context
    created = client.post("/api/v1/markdown-cleaning/tasks", json=payload).json()
    task_id = uuid.UUID(created["taskId"])
    task = session.get(MarkdownCleaningTask, task_id)
    assert task is not None

    task.status = MarkdownCleaningTaskStatus.SUCCEEDED
    task.file_oss_url = "https://oss.internal/sample.md"
    task.processing_phase = None
    task.started_at = datetime(2025, 1, 1, tzinfo=UTC)
    task.finished_at = datetime(2025, 1, 1, 0, 0, 5, tzinfo=UTC)
    task.duplicate_paragraphs_removed = 3
    task.phone_redaction_count = 2
    task.id_card_redaction_count = 1
    task.bank_card_redaction_count = 0
    task.email_redaction_count = 4
    task.ipv4_redaction_count = 1
    task.formatting_change_count = 12
    session.add(task)
    session.commit()

    response = client.get(f"/api/v1/markdown-cleaning/tasks/{task_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "succeeded"
    assert body["result"] == {
        "fileId": "11",
        "fileStoragePath": str(Path(str(payload["fileStoragePath"]))),
        "fileOssUrl": "https://oss.internal/sample.md",
        "targetPath": str(Path(str(payload["targetPath"]))),
        "summary": {
            "duplicateParagraphsRemoved": 3,
            "redactions": {
                "phone": 2,
                "idCard": 1,
                "bankCard": 0,
                "email": 4,
                "ipv4": 1,
            },
            "formattingChanges": 12,
        },
    }


def test_get_maps_failed_error_only(
    api_context: tuple[
        TestClient, Session, User, RecordingDispatcher, dict[str, str | None]
    ],
) -> None:
    client, session, _user, _dispatcher, payload = api_context
    created = client.post("/api/v1/markdown-cleaning/tasks", json=payload).json()
    task_id = uuid.UUID(created["taskId"])
    task = session.get(MarkdownCleaningTask, task_id)
    assert task is not None

    task.status = MarkdownCleaningTaskStatus.FAILED
    task.error_code = MarkdownCleaningApiErrorCode.INPUT_PATH_NOT_ALLOWED
    task.error_message = "输入路径不合法"
    task.started_at = datetime.now(UTC)
    task.finished_at = datetime.now(UTC)
    session.add(task)
    session.commit()

    response = client.get(f"/api/v1/markdown-cleaning/tasks/{task_id}")
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "failed"
    assert body["result"] is None
    assert body["error"] == {
        "code": "INPUT_PATH_NOT_ALLOWED",
        "message": "输入路径不合法",
    }


def test_get_other_caller_and_missing_task_returns_same_404(
    api_context: tuple[
        TestClient, Session, User, RecordingDispatcher, dict[str, str | None]
    ],
) -> None:
    client, _session, user, _dispatcher, payload = api_context
    created = client.post("/api/v1/markdown-cleaning/tasks", json=payload).json()

    app.dependency_overrides[get_current_user] = lambda: User(
        id=uuid.uuid4(),
        email="other@example.com",
        hashed_password="not-used",
    )
    hidden = client.get(f"/api/v1/markdown-cleaning/tasks/{created['taskId']}")
    missing = client.get(f"/api/v1/markdown-cleaning/tasks/{uuid.uuid4()}")

    assert hidden.status_code == missing.status_code == 404
    assert hidden.json() == {
        "detail": {
            "code": "TASK_NOT_FOUND",
            "message": "任务不存在",
        }
    }
    assert missing.json() == hidden.json()
    assert user.id != app.dependency_overrides[get_current_user]().id


def test_endpoints_require_authentication() -> None:
    app.dependency_overrides.clear()
    payload = {
        "sessionId": "s1",
        "fileId": "11",
        "fileStoragePath": "C:/input/source.md",
        "fileOssUrl": None,
        "targetPath": "C:/output/result.md",
    }
    with TestClient(app) as client:
        post_response = client.post("/api/v1/markdown-cleaning/tasks", json=payload)
        get_response = client.get(f"/api/v1/markdown-cleaning/tasks/{uuid.uuid4()}")

    assert post_response.status_code == 401
    assert get_response.status_code == 401
