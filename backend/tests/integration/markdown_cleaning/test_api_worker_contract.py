from __future__ import annotations

import json
import uuid

import redis
from celery import Celery
from kombu import Connection

from app.features.markdown_cleaning.dispatcher import (
    CeleryMarkdownCleaningTaskDispatcher,
)
from tests.integration.markdown_cleaning.conftest import REDIS_URL


def test_real_redis_broker_envelope_contains_only_authoritative_identifiers() -> None:
    client = redis.Redis.from_url(REDIS_URL)
    client.flushdb()
    celery = Celery("task6-contract", broker=REDIS_URL)
    task_id = uuid.uuid4()
    CeleryMarkdownCleaningTaskDispatcher(celery).enqueue_execute(task_id)
    with Connection(REDIS_URL) as connection:
        queue = connection.SimpleQueue("markdown_cleaning")
        message = queue.get(block=True, timeout=5)
        body = message.payload
        message.ack()
    assert body[1] == {
        "taskId": str(task_id),
        "taskType": "markdown_cleaning",
        "schemaVersion": 1,
    }
    serialized = json.dumps(body, default=str)
    assert "targetPath" not in serialized and "staging" not in serialized


def test_public_contract_source_never_serializes_staging_path() -> None:
    from app.features.markdown_cleaning.routes import task_to_public
    from app.features.markdown_cleaning.state_machine import MarkdownCleaningTaskStatus
    from app.features.markdown_cleaning.task_models import MarkdownCleaningTask

    task = MarkdownCleaningTask(
        caller_id=uuid.uuid4(),
        session_id="s",
        file_id="f.md",
        request_fingerprint="a" * 64,
        file_storage_path="C:/business/in.md",
        selected_input_type="local",
        target_path="C:/business/out.md",
        status=MarkdownCleaningTaskStatus.SUCCEEDED,
        staging_path="C:/secret/staging/result.md",
        duplicate_paragraphs_removed=0,
        phone_redaction_count=0,
        id_card_redaction_count=0,
        bank_card_redaction_count=0,
        email_redaction_count=0,
        ipv4_redaction_count=0,
        formatting_change_count=0,
    )
    payload = task_to_public(task).model_dump(by_alias=True, mode="json")
    assert payload["result"]["targetPath"] == "C:/business/out.md"
    assert "staging" not in json.dumps(payload).lower()
