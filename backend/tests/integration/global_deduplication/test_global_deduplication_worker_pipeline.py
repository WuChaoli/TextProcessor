import hashlib
import json
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
from sqlmodel import Session, SQLModel, create_engine
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

os.environ.setdefault("PROJECT_NAME", "TextProcessor test")
os.environ.setdefault("POSTGRES_SERVER", "localhost")
os.environ.setdefault("POSTGRES_USER", "test")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("POSTGRES_DB", "test")
os.environ.setdefault("FIRST_SUPERUSER", "admin@example.com")
os.environ.setdefault("FIRST_SUPERUSER_PASSWORD", "test-password")

from app.core.config import GlobalDeduplicationWorkerSettings
from app.features.global_deduplication.celery_tasks import (
    build_orchestrator,
    handle_poll_task,
    handle_submit_task,
)
from app.features.global_deduplication.repository import (
    GlobalDeduplicationTaskRepository,
)
from app.features.global_deduplication.state_machine import (
    GlobalDeduplicationTaskStatus,
)
from app.features.global_deduplication.task_models import GlobalDeduplicationTask
from app.models import User
from tests.features.global_deduplication.test_celery_tasks import message


@dataclass
class RecordingScheduler:
    submits: list[tuple[uuid.UUID, int]] = field(default_factory=list)
    polls: list[tuple[uuid.UUID, int]] = field(default_factory=list)

    def enqueue_submit(self, task_id: uuid.UUID, *, countdown: int) -> None:
        self.submits.append((task_id, countdown))

    def enqueue_poll(self, task_id: uuid.UUID, *, countdown: int) -> None:
        self.polls.append((task_id, countdown))


@pytest.mark.container_integration
def test_worker_pipeline_uses_postgres_and_redis_containers(
    tmp_path: Path,
) -> None:
    batch_root = tmp_path / "batch"
    original_root = batch_root / "original"
    duplicate_root = batch_root / "duplicate"
    staging_root = tmp_path / "staging"
    original_root.mkdir(parents=True)
    duplicate_root.mkdir()
    first = original_root / "one.md"
    second = original_root / "two.txt"
    skipped = original_root / "ignored.pdf"
    first.write_text("same content", encoding="utf-8")
    second.write_text("same content", encoding="utf-8")
    skipped.write_bytes(b"not a supported document")
    caller_id = uuid.uuid7()
    task_id = uuid.uuid7()
    job_id = uuid.uuid7()
    scheduler = RecordingScheduler()
    output_path: Path | None = None

    def datajuicer(request: httpx.Request) -> httpx.Response:
        nonlocal output_path
        if request.method == "POST":
            body = json.loads(request.read())
            output_path = Path(body["outputPath"])
            output_path.write_text(
                (
                    '{"uid":0,"clusterId":"c1","representative":true,'
                    '"method":"exact"}\n'
                    '{"uid":1,"clusterId":"c1","representative":false,'
                    '"method":"exact"}\n'
                ),
                encoding="utf-8",
            )
            return httpx.Response(
                202,
                json={
                    "jobId": str(job_id),
                    "requestId": str(task_id),
                    "profile": "text_exact_minhash_v1",
                    "status": "queued",
                },
            )
        assert output_path is not None
        digest = hashlib.sha256(output_path.read_bytes()).hexdigest()
        return httpx.Response(
            200,
            json={
                "jobId": str(job_id),
                "requestId": str(task_id),
                "profile": "text_exact_minhash_v1",
                "status": "succeeded",
                "progress": {
                    "phase": "completed",
                    "total": 2,
                    "processed": 2,
                    "percent": 100,
                },
                "result": {
                    "outputPath": str(output_path),
                    "outputSha256": digest,
                },
                "error": None,
            },
        )

    configured = GlobalDeduplicationWorkerSettings(
        staging_root=staging_root,
        datajuicer_base_url="http://datajuicer.internal",
        datajuicer_poll_initial_delay_seconds=1,
    )
    with (
        PostgresContainer("postgres:16-alpine") as postgres,
        RedisContainer("redis:7-alpine") as redis,
    ):
        database_url = postgres.get_connection_url().replace(
            "postgresql+psycopg2", "postgresql+psycopg"
        ).replace("@localhost:", "@127.0.0.1:")
        container_engine = create_engine(database_url)
        SQLModel.metadata.create_all(container_engine)
        redis_client = redis.get_client()
        try:
            assert redis_client.ping() is True
        finally:
            redis_client.close()
        with (
            Session(container_engine) as session,
            httpx.Client(transport=httpx.MockTransport(datajuicer)) as client,
        ):
            session.add(
                User(
                    id=caller_id,
                    email=f"global-worker-{caller_id}@example.com",
                    hashed_password="not-used",
                )
            )
            session.commit()
            session.add(
                GlobalDeduplicationTask(
                    id=task_id,
                    caller_id=caller_id,
                    session_id=f"session-{task_id}",
                    request_fingerprint="a" * 64,
                    input_path=str(batch_root),
                    status=GlobalDeduplicationTaskStatus.QUEUED,
                )
            )
            session.commit()
            orchestrator = build_orchestrator(
                session,
                http_client=client,
                worker_settings=configured,
                scheduler=scheduler,
            )

            handle_submit_task(message(task_id), orchestrator=orchestrator)
            saved = GlobalDeduplicationTaskRepository(session).get(task_id)
            assert saved is not None
            assert saved.status is GlobalDeduplicationTaskStatus.RUNNING
            assert scheduler.polls == [(task_id, 1)]

            saved.next_poll_at = None
            session.add(saved)
            session.commit()
            handle_poll_task(message(task_id), orchestrator=orchestrator)

            completed = GlobalDeduplicationTaskRepository(session).get(task_id)
            assert completed is not None
            assert completed.status is GlobalDeduplicationTaskStatus.SUCCEEDED
            assert completed.result_metadata == {
                "total_files": 2,
                "unique_files": 1,
                "moved_duplicates": 1,
                "move_failures": [],
            }
    assert first.exists()
    assert not second.exists()
    assert (duplicate_root / "two.txt").read_text(encoding="utf-8") == "same content"
    assert skipped.exists()
