from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlmodel import Session

from app.features.markdown_cleaning.orchestration import MarkdownCleaningRecovery
from app.features.markdown_cleaning.publisher import MarkdownCleaningResultPublisher
from app.features.markdown_cleaning.repository import MarkdownCleaningTaskRepository
from app.features.markdown_cleaning.state_machine import MarkdownCleaningTaskStatus
from tests.integration.markdown_cleaning.conftest import persist_queued_task


class RecordingDispatcher:
    def __init__(self) -> None:
        self.ids = []

    def enqueue_execute(self, task_id) -> None:
        self.ids.append(task_id)


def test_expired_worker_lease_is_requeued_for_takeover(
    pg_session: Session, caller, pipeline_roots: dict[str, Path]
) -> None:
    source = pipeline_roots["input"] / "lease.md"
    source.write_text("lease\n", encoding="utf-8")
    task = persist_queued_task(
        pg_session, caller, source=source, target=pipeline_roots["output"] / "lease.md"
    )
    repository = MarkdownCleaningTaskRepository(pg_session)
    old = datetime.now(UTC) - timedelta(minutes=5)
    claimed = repository.acquire_queued(
        task.id, now=old, lease_seconds=1, processing_timeout_seconds=30
    )
    assert claimed is not None
    dispatcher = RecordingDispatcher()
    recovery = MarkdownCleaningRecovery(
        repository=repository,
        dispatcher=dispatcher,
        publisher=MarkdownCleaningResultPublisher(
            output_roots=(pipeline_roots["output"],), max_output_bytes=1024
        ),
        queue_recovery_interval_seconds=1,
        batch_size=10,
    )
    recovery.recover_batch()
    saved = repository.get(task.id)
    assert saved.status is MarkdownCleaningTaskStatus.QUEUED
    assert dispatcher.ids == [task.id]


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
