import uuid
from typing import Any

from app.features.markdown_cleaning.dispatcher import (
    CeleryMarkdownCleaningTaskDispatcher,
)
from app.features.markdown_cleaning.messages import MarkdownCleaningMessage


class FakeCelery:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []

    def send_task(
        self, name: str, *, kwargs: dict[str, Any], queue: str | None = None
    ) -> None:
        self.calls.append((name, kwargs, queue))


def test_markdown_cleaning_message_serializes_minimal_identity() -> None:
    payload = MarkdownCleaningMessage(
        taskId=uuid.UUID("019fb000-0000-7000-8000-000000000001"),
        taskType="markdown_cleaning",
        schemaVersion=1,
    ).as_payload()

    assert payload == {
        "taskId": "019fb000-0000-7000-8000-000000000001",
        "taskType": "markdown_cleaning",
        "schemaVersion": 1,
    }


def test_markdown_cleaning_message_only_contains_minimal_identity_fields() -> None:
    payload = MarkdownCleaningMessage(
        taskId=uuid.UUID("019fb000-0000-7000-8000-000000000001"),
        taskType="markdown_cleaning",
        schemaVersion=1,
    ).as_payload()

    assert set(payload.keys()) == {"taskId", "taskType", "schemaVersion"}


def test_celery_dispatcher_sends_minimal_payload() -> None:
    celery = FakeCelery()
    dispatcher = CeleryMarkdownCleaningTaskDispatcher(celery)
    task_id = uuid.uuid4()

    dispatcher.enqueue_execute(task_id)

    assert celery.calls == [
        (
            "markdown_cleaning.execute",
            {
                "taskId": str(task_id),
                "taskType": "markdown_cleaning",
                "schemaVersion": 1,
            },
            "markdown_cleaning",
        )
    ]
