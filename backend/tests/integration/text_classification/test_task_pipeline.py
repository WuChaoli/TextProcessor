import json
import os
import uuid

import pytest
from celery import Celery
from kombu import Connection
from sqlmodel import Session

from app.core.db import engine
from app.features.text_classification import celery_tasks
from app.features.text_classification.celery_tasks import execute
from app.features.text_classification.dispatcher import CeleryClassificationDispatcher
from app.features.text_classification.input_preparer import PreparedClassificationInput
from app.features.text_classification.models import ClassificationTask
from app.features.text_classification.repository import ClassificationTaskRepository
from app.features.text_classification.schemas import ClassificationTaskCreate
from app.features.text_classification.service import ClassificationTaskService
from app.models import User
from app.tasking.envelope import TaskEnvelope
from app.tasking.state import TaskStatus

pytestmark = pytest.mark.real_integration


def test_real_postgres_and_redis_pipeline_contains_only_task_envelope(monkeypatch) -> None:
    redis_url = os.environ["CLASSIFICATION_TEST_REDIS_URL"]
    celery = Celery("classification-integration", broker=redis_url, set_as_current=False)
    caller_id = uuid.uuid4()
    task_id: uuid.UUID | None = None
    with Session(engine) as session:
        session.add(User(id=caller_id, email=f"classification-{caller_id}@example.com", hashed_password="unused"))
        session.commit()
        try:
            service = ClassificationTaskService(
                ClassificationTaskRepository(session),
                CeleryClassificationDispatcher(celery),
            )
            task = service.create_task(
                caller_id,
                ClassificationTaskCreate(
                    session_id="integration-session",
                    file_id="integration-file",
                    input_uri="file:///allowed/input.txt",
                ),
            )
            task_id = task.id

            with Connection(redis_url) as connection:
                queue = connection.SimpleQueue("text_classification")
                message = queue.get(block=True, timeout=5)
                payload = message.payload
                message.ack()

            assert payload[1] == {
                "task_id": str(task.id),
                "task_type": "text_classification",
                "schema_version": 1,
            }
            assert "input_uri" not in json.dumps(payload)
            envelope = TaskEnvelope.parse(
                payload[1],
                expected_type="text_classification",
                expected_schema_version=1,
            )

            class FakePreparer:
                def __init__(self, **_kwargs: object) -> None:
                    pass

                def prepare(self, task_id: str, _uri: str) -> PreparedClassificationInput:
                    return PreparedClassificationInput(
                        f"file:///shared/{task_id}/input.txt",
                        "c" * 64,
                        16,
                    )

            class FakeClient:
                def __init__(self, **_kwargs: object) -> None:
                    pass

                def classify(self, request_id: str, _uri: str) -> dict[str, object]:
                    return {
                        "schemaVersion": "1",
                        "requestId": request_id,
                        "tags": ["a", "b", "c", "d"],
                        "confidence": {"topTriple": 0.9, "endDoc": 0.8},
                        "releaseId": "release-1",
                    }

            monkeypatch.setattr(celery_tasks, "ClassificationInputPreparer", FakePreparer)
            monkeypatch.setattr(celery_tasks, "ClassificationClient", FakeClient)
            execute(session, envelope.task_id)

            session.expire_all()
            completed = session.get(ClassificationTask, task.id)
            assert completed is not None
            assert completed.status is TaskStatus.SUCCEEDED
            assert completed.attempt_count == 1
            assert completed.input_sha256 == "c" * 64
        finally:
            if task_id is not None:
                task = session.get(ClassificationTask, task_id)
                if task is not None:
                    session.delete(task)
                    session.commit()
            user = session.get(User, caller_id)
            if user is not None:
                session.delete(user)
                session.commit()
