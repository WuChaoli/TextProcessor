import hashlib
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from sqlmodel import Session, delete

from app.core.config import GlobalDeduplicationWorkerSettings
from app.core.db import engine
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


def test_worker_pipeline_uses_postgres_files_and_fake_http(
    tmp_path: Path,
) -> None:
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    staging_root = tmp_path / "staging"
    input_root.mkdir()
    output_root.mkdir()
    first = input_root / "one.md"
    second = input_root / "two.txt"
    first.write_text("same content", encoding="utf-8")
    second.write_text("same content", encoding="utf-8")
    manifest = input_root / "manifest.json"
    manifest.write_text(
        json.dumps(
            [
                {"fileId": "1", "fileStoragePath": str(first)},
                {"fileId": "2", "fileStoragePath": str(second)},
            ]
        ),
        encoding="utf-8",
    )
    target = output_root / "result.json"
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
        output_roots=(output_root,),
        datajuicer_base_url="http://datajuicer.internal",
        datajuicer_poll_initial_delay_seconds=1,
    )
    with (
        Session(engine) as session,
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
                input_json_path=str(manifest),
                target_path=str(target),
                status=GlobalDeduplicationTaskStatus.QUEUED,
            )
        )
        session.commit()
        orchestrator = build_orchestrator(
            session,
            http_client=client,
            worker_settings=configured,
            input_roots=(input_root,),
            scheduler=scheduler,
        )
        try:
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
            result = json.loads(target.read_text(encoding="utf-8"))
            assert [record["keep"] for record in result] == [True, False]
            assert result[0]["groupId"] == result[1]["groupId"]
        finally:
            session.exec(
                delete(GlobalDeduplicationTask).where(
                    GlobalDeduplicationTask.id == task_id
                )
            )
            session.exec(delete(User).where(User.id == caller_id))
            session.commit()
