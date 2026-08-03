from __future__ import annotations

import hashlib
import json
import socket
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from celery import Celery
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import MarkdownCleaningWorkerSettings, settings
from app.features.markdown_cleaning.celery_tasks import execute_markdown_cleaning_task
from app.features.markdown_cleaning.orchestration import MarkdownCleaningRecovery
from app.features.markdown_cleaning.publisher import MarkdownCleaningResultPublisher
from app.features.markdown_cleaning.repository import MarkdownCleaningTaskRepository
from app.features.markdown_cleaning.state_machine import MarkdownCleaningTaskStatus
from tests.integration.markdown_cleaning.conftest import persist_queued_task


def test_real_worker_crash_is_recovered_by_real_recover_task(
    pg_session: Session, markdown_cleaning_runtime
) -> None:
    runtime = markdown_cleaning_runtime
    hostname = "host.docker.internal"
    address = socket.gethostbyname(hostname)
    first_request = threading.Event()
    release_first = threading.Event()
    request_count = 0

    class BlockingMarkdownHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            nonlocal request_count
            request_count += 1
            if request_count == 1:
                first_request.set()
                release_first.wait(timeout=30)
            body = b"# crash takeover\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except ConnectionError:
                pass

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("0.0.0.0", 80), BlockingMarkdownHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    worker_config = {
        "staging_root": str(runtime.staging_root),
        "output_roots": [str(runtime.target.parent)],
        "allowed_http_hosts": [hostname],
        "allowed_http_cidrs": [f"{address}/32"],
        "queue_lease_seconds": 2,
        "queue_recovery_interval_seconds": 1,
        "processing_soft_timeout_seconds": 30,
        "processing_hard_timeout_seconds": 60,
    }
    runtime._worker_env["MARKDOWN_CLEANING_HTTP_ALLOWED_HOSTS"] = json.dumps([hostname])
    runtime._worker_env["MARKDOWN_CLEANING_HTTP_ALLOWED_CIDRS"] = json.dumps(
        [f"{address}/32"]
    )
    runtime._worker_env["MARKDOWN_CLEANING_WORKER"] = json.dumps(worker_config)
    settings.MARKDOWN_CLEANING_HTTP_ALLOWED_HOSTS = [hostname]
    settings.MARKDOWN_CLEANING_HTTP_ALLOWED_CIDRS = [f"{address}/32"]
    settings.MARKDOWN_CLEANING_WORKER = MarkdownCleaningWorkerSettings(**worker_config)
    try:
        with TestClient(runtime.app) as client:
            response = client.post(
                "/api/v1/markdown-cleaning/tasks",
                json={
                    "sessionId": runtime.session_id,
                    "fileId": "crash.md",
                    "fileOssUrl": f"http://{hostname}/crash.md",
                    "targetPath": str(runtime.target),
                },
            )
            assert response.status_code == 202, response.text
            task_id = response.json()["taskId"]
            with runtime.worker():
                assert first_request.wait(timeout=20)
                deadline = time.monotonic() + 10
                while True:
                    pg_session.expire_all()
                    running = MarkdownCleaningTaskRepository(pg_session).get(task_id)
                    assert running is not None
                    if running.status is MarkdownCleaningTaskStatus.RUNNING:
                        break
                    assert time.monotonic() < deadline
                    time.sleep(0.05)
            assert not runtime.target.exists()
            release_first.set()
            time.sleep(2.5)
            Celery(
                "crash-recovery",
                broker=runtime.redis_url,
                set_as_current=False,
            ).send_task("markdown_cleaning.recover", queue="markdown_cleaning")
            with runtime.worker():
                deadline = time.monotonic() + 45
                while True:
                    result = client.get(
                        f"/api/v1/markdown-cleaning/tasks/{task_id}"
                    ).json()
                    if result["status"] == "succeeded":
                        break
                    assert time.monotonic() < deadline, result
                    time.sleep(0.2)
        pg_session.expire_all()
        saved = MarkdownCleaningTaskRepository(pg_session).get(task_id)
        assert saved is not None and saved.attempt_count == 2
        assert saved.started_at is not None and saved.processing_deadline is not None
        started_at = saved.started_at.replace(tzinfo=UTC)
        processing_deadline = saved.processing_deadline.replace(tzinfo=UTC)
        assert 29 <= (processing_deadline - started_at).total_seconds() <= 31
        assert execute_markdown_cleaning_task.time_limit is not None
        assert (
            execute_markdown_cleaning_task.time_limit
            > settings.MARKDOWN_CLEANING_WORKER.processing_soft_timeout_seconds
        )
        assert runtime.target.read_bytes() == b"# crash takeover\n"
        assert request_count == 2
        time.sleep(1)
        assert runtime.target.read_bytes() == b"# crash takeover\n"
    finally:
        release_first.set()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)


class RecordingDispatcher:
    def __init__(self) -> None:
        self.ids = []

    def enqueue_execute(self, task_id) -> None:
        self.ids.append(task_id)


def test_expired_worker_lease_is_requeued_for_takeover(
    pg_session: Session, caller, markdown_cleaning_runtime
) -> None:
    runtime = markdown_cleaning_runtime
    source = runtime.source.parent / "lease.md"
    source.write_text("lease\n", encoding="utf-8")
    task = persist_queued_task(
        pg_session, caller, source=source, target=runtime.target.parent / "lease.md"
    )
    repository = MarkdownCleaningTaskRepository(pg_session)
    old = datetime.now(UTC) - timedelta(seconds=5)
    claimed = repository.acquire_queued(
        task.id, now=old, lease_seconds=1, processing_timeout_seconds=30
    )
    assert claimed is not None
    worker = runtime.worker()
    with worker:
        Celery(
            "recovery-test", broker=runtime.redis_url, set_as_current=False
        ).send_task("markdown_cleaning.recover", queue="markdown_cleaning")
        deadline = time.monotonic() + 45
        while True:
            pg_session.expire_all()
            saved = repository.get(task.id)
            if saved.status is MarkdownCleaningTaskStatus.SUCCEEDED:
                break
            assert time.monotonic() < deadline, saved.status
            time.sleep(0.2)
    assert saved.attempt_count == 2


def test_published_file_is_reconciled_after_database_failure(
    pg_session: Session, caller, pipeline_roots: dict[str, Path]
) -> None:
    source = pipeline_roots["input"] / "recover.md"
    source.write_text("source\n", encoding="utf-8")
    target = pipeline_roots["output"] / "recover.md"
    task = persist_queued_task(pg_session, caller, source=source, target=target)
    repository = MarkdownCleaningTaskRepository(pg_session)
    old = datetime.now(UTC) - timedelta(minutes=5)
    claimed = repository.acquire_queued(
        task.id, now=old, lease_seconds=1, processing_timeout_seconds=30
    )
    prepared = pipeline_roots["staging"] / str(task.id) / "output" / "result.md"
    prepared.parent.mkdir(parents=True)
    prepared.write_bytes(b"clean\n")
    digest = hashlib.sha256(b"clean\n").hexdigest()
    repository.save_prepared(
        task.id,
        lease_token=claimed.lease_token,
        staging_path=str(prepared),
        input_sha256="a" * 64,
        prepared_output_sha256=digest,
        duplicate_paragraphs_removed=0,
        phone_redaction_count=0,
        id_card_redaction_count=0,
        bank_card_redaction_count=0,
        email_redaction_count=0,
        ipv4_redaction_count=0,
        formatting_change_count=0,
        now=old,
    )
    target.write_bytes(b"clean\n")
    recovery = MarkdownCleaningRecovery(
        repository=repository,
        dispatcher=RecordingDispatcher(),
        publisher=MarkdownCleaningResultPublisher(
            output_roots=(pipeline_roots["output"],), max_output_bytes=1024
        ),
        queue_recovery_interval_seconds=1,
        batch_size=10,
    )
    recovery.recover_batch()
    saved = repository.get(task.id)
    assert saved.status is MarkdownCleaningTaskStatus.SUCCEEDED
    assert saved.output_sha256 == digest
