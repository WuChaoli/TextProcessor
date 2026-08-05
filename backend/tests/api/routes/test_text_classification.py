import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.api.deps import get_current_user, get_db
from app.features.text_classification.routes import get_classification_dispatcher
from app.main import app
from app.models import User


class RecordingDispatcher:
    def __init__(self) -> None:
        self.task_ids: list[uuid.UUID] = []

    def enqueue(self, task_id: uuid.UUID) -> None:
        self.task_ids.append(task_id)


@pytest.fixture
def context() -> Generator[tuple[TestClient, RecordingDispatcher]]:
    test_engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(test_engine)
    session = Session(test_engine)
    user = User(id=uuid.uuid4(), email="classification@example.com", hashed_password="unused")
    dispatcher = RecordingDispatcher()
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_classification_dispatcher] = lambda: dispatcher
    with TestClient(app) as client:
        yield client, dispatcher
    app.dependency_overrides.clear()
    session.close()


def test_create_and_get_are_task_id_contracts(context: tuple[TestClient, RecordingDispatcher]) -> None:
    client, dispatcher = context
    payload = {"sessionId": "s-1", "fileId": "f-1", "inputUri": "file:///input/a.txt"}

    first = client.post("/api/v1/text-classification/tasks", json=payload)
    second = client.post("/api/v1/text-classification/tasks", json=payload)

    assert first.status_code == 202
    assert first.json() == second.json()
    assert first.json()["status"] == "queued"
    assert len(dispatcher.task_ids) == 1
    fetched = client.get(f"/api/v1/text-classification/tasks/{first.json()['taskId']}")
    assert fetched.status_code == 200
    assert fetched.json()["result"] is None
    assert fetched.json()["error"] is None


def test_changed_uri_conflicts_with_idempotency_key(context: tuple[TestClient, RecordingDispatcher]) -> None:
    client, _ = context
    base = {"sessionId": "s-2", "fileId": "f-2", "inputUri": "file:///input/a.txt"}
    assert client.post("/api/v1/text-classification/tasks", json=base).status_code == 202
    changed = {**base, "inputUri": "file:///input/b.txt"}
    response = client.post("/api/v1/text-classification/tasks", json=changed)
    assert response.status_code == 409


def test_task_is_hidden_from_another_caller(context: tuple[TestClient, RecordingDispatcher]) -> None:
    client, _ = context
    created = client.post(
        "/api/v1/text-classification/tasks",
        json={"sessionId": "s-private", "fileId": "f-private", "inputUri": "file:///input/a.txt"},
    ).json()
    other = User(id=uuid.uuid4(), email="other@example.com", hashed_password="unused")
    app.dependency_overrides[get_current_user] = lambda: other

    response = client.get(f"/api/v1/text-classification/tasks/{created['taskId']}")

    assert response.status_code == 404
