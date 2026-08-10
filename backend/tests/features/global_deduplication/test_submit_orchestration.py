import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.core.config import GlobalDeduplicationWorkerSettings
from app.features.global_deduplication.adapters.datajuicer import (
    DataJuicerSubmission,
    DataJuicerSubmitRequest,
)
from app.features.global_deduplication.errors import (
    GlobalDeduplicationErrorCode,
    GlobalDeduplicationProcessingError,
)
from app.features.global_deduplication.input_reader import BoundedUriReader
from app.features.global_deduplication.orchestration import (
    GlobalDeduplicationOrchestrator,
)
from app.features.global_deduplication.repository import (
    GlobalDeduplicationTaskRepository,
)
from app.features.global_deduplication.staging import GlobalDeduplicationStaging
from app.features.global_deduplication.state_machine import (
    GlobalDeduplicationTaskStatus,
)
from app.features.global_deduplication.task_models import GlobalDeduplicationTask
from app.models import User  # noqa: F401


@pytest.fixture(autouse=True)
def db() -> None:
    """编排单测使用文件内构造的 SQLite，不依赖全局 PostgreSQL。"""


NOW = datetime(2026, 7, 31, 10, 0, tzinfo=UTC)


@dataclass
class FakeAdapter:
    submissions: list[DataJuicerSubmitRequest] = field(default_factory=list)
    error: GlobalDeduplicationProcessingError | None = None
    job_id: uuid.UUID = field(default_factory=uuid.uuid7)

    def submit(self, request: DataJuicerSubmitRequest) -> DataJuicerSubmission:
        self.submissions.append(request)
        if self.error is not None:
            raise self.error
        return DataJuicerSubmission(
            job_id=self.job_id,
            request_id=request.request_id,
            profile=request.profile,
            status="queued",
        )


@dataclass
class FakeScheduler:
    submits: list[tuple[uuid.UUID, int]] = field(default_factory=list)
    polls: list[tuple[uuid.UUID, int]] = field(default_factory=list)

    def enqueue_submit(self, task_id: uuid.UUID, *, countdown: int) -> None:
        self.submits.append((task_id, countdown))

    def enqueue_poll(self, task_id: uuid.UUID, *, countdown: int) -> None:
        self.polls.append((task_id, countdown))


def build_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def build_task(
    session: Session,
    *,
    manifest_path: Path,
    target_path: Path,
) -> GlobalDeduplicationTask:
    del target_path
    task = GlobalDeduplicationTask(
        caller_id=uuid.uuid4(),
        session_id=str(uuid.uuid7()),
        request_fingerprint="a" * 64,
        input_path=str(manifest_path.parent),
        status=GlobalDeduplicationTaskStatus.QUEUED,
        queued_at=NOW,
    )
    session.add(task)
    session.commit()
    return task


def build_orchestrator(
    session: Session,
    *,
    input_root: Path,
    staging_root: Path,
    adapter: FakeAdapter,
    scheduler: FakeScheduler,
    now: datetime = NOW,
) -> GlobalDeduplicationOrchestrator:
    settings = GlobalDeduplicationWorkerSettings(
        staging_root=staging_root,
        max_documents=10,
        max_manifest_bytes=1024,
        max_document_bytes=1024,
        max_total_bytes=4096,
        datajuicer_poll_initial_delay_seconds=7,
    )
    return GlobalDeduplicationOrchestrator(
        repository=GlobalDeduplicationTaskRepository(session),
        reader=BoundedUriReader(input_roots=(input_root,), chunk_bytes=16),
        staging=GlobalDeduplicationStaging(staging_root),
        adapter=adapter,
        scheduler=scheduler,
        settings=settings,
        now=lambda: now,
    )


def prepare_manifest(tmp_path: Path) -> tuple[Path, Path]:
    batch = tmp_path / "batch"
    source = batch / "original"
    source.mkdir(parents=True)
    (batch / "duplicate").mkdir()
    document = source / "one.md"
    document.write_bytes(b"\xef\xbb\xbfline\r\n")
    manifest = batch / "manifest.json"
    manifest.write_text("[]", encoding="utf-8")
    return source, manifest


def test_submit_prepares_documents_and_external_job(tmp_path: Path) -> None:
    session = build_session()
    source, manifest = prepare_manifest(tmp_path)
    adapter = FakeAdapter()
    scheduler = FakeScheduler()
    task = build_task(
        session,
        manifest_path=manifest,
        target_path=tmp_path / "published" / "result.json",
    )
    orchestrator = build_orchestrator(
        session,
        input_root=source,
        staging_root=tmp_path / "staging",
        adapter=adapter,
        scheduler=scheduler,
    )

    orchestrator.submit(task.id)

    request = adapter.submissions[0]
    assert request.request_id == task.id
    assert request.input_path.name == "input.jsonl"
    assert request.output_path.name == "datajuicer-result.jsonl"
    saved = GlobalDeduplicationTaskRepository(session).get(task.id)
    assert saved is not None
    assert saved.external_job_id == adapter.job_id
    assert saved.processing_phase == "deduplicating"
    assert scheduler.polls == [(task.id, 7)]


def test_duplicate_submit_does_not_repeat_external_job(tmp_path: Path) -> None:
    session = build_session()
    source, manifest = prepare_manifest(tmp_path)
    adapter = FakeAdapter()
    scheduler = FakeScheduler()
    task = build_task(
        session,
        manifest_path=manifest,
        target_path=tmp_path / "published" / "output.json",
    )
    orchestrator = build_orchestrator(
        session,
        input_root=source,
        staging_root=tmp_path / "staging",
        adapter=adapter,
        scheduler=scheduler,
    )

    orchestrator.submit(task.id)
    orchestrator.submit(task.id)

    assert len(adapter.submissions) == 1


def test_input_error_fails_without_external_submission(tmp_path: Path) -> None:
    session = build_session()
    batch = tmp_path / "batch"
    source = batch / "original"
    source.mkdir(parents=True)
    (batch / "duplicate").mkdir()
    manifest = batch / "manifest.json"
    manifest.write_text("[]", encoding="utf-8")
    adapter = FakeAdapter()
    scheduler = FakeScheduler()
    task = build_task(
        session,
        manifest_path=manifest,
        target_path=tmp_path / "published" / "output.json",
    )
    orchestrator = build_orchestrator(
        session,
        input_root=source,
        staging_root=tmp_path / "staging",
        adapter=adapter,
        scheduler=scheduler,
    )

    orchestrator.submit(task.id)

    saved = GlobalDeduplicationTaskRepository(session).get(task.id)
    assert saved is not None
    assert saved.status is GlobalDeduplicationTaskStatus.FAILED
    assert saved.error_code == GlobalDeduplicationErrorCode.EMPTY_DOCUMENT_LIST
    assert adapter.submissions == []


def test_existing_unrelated_file_does_not_block_submission(tmp_path: Path) -> None:
    session = build_session()
    source, manifest = prepare_manifest(tmp_path)
    target = tmp_path / "published" / "output.json"
    target.parent.mkdir()
    target.write_text("sentinel", encoding="utf-8")
    adapter = FakeAdapter()
    task = build_task(session, manifest_path=manifest, target_path=target)
    orchestrator = build_orchestrator(
        session,
        input_root=source,
        staging_root=tmp_path / "staging",
        adapter=adapter,
        scheduler=FakeScheduler(),
    )

    orchestrator.submit(task.id)

    saved = GlobalDeduplicationTaskRepository(session).get(task.id)
    assert saved is not None
    assert saved.error_code is None
    assert target.read_text(encoding="utf-8") == "sentinel"


def test_uncertain_submission_remains_recoverable(tmp_path: Path) -> None:
    session = build_session()
    source, manifest = prepare_manifest(tmp_path)
    adapter = FakeAdapter(
        error=GlobalDeduplicationProcessingError(
            GlobalDeduplicationErrorCode.PROCESSOR_SUBMISSION_UNCERTAIN,
            "uncertain",
            transient=True,
        )
    )
    task = build_task(
        session,
        manifest_path=manifest,
        target_path=tmp_path / "published" / "output.json",
    )
    orchestrator = build_orchestrator(
        session,
        input_root=source,
        staging_root=tmp_path / "staging",
        adapter=adapter,
        scheduler=FakeScheduler(),
    )

    orchestrator.submit(task.id)

    saved = GlobalDeduplicationTaskRepository(session).get(task.id)
    assert saved is not None
    assert saved.status is GlobalDeduplicationTaskStatus.RUNNING
    assert saved.external_job_id is None
    assert saved.lease_expires_at is not None
    assert saved.lease_expires_at.replace(tzinfo=UTC) == NOW
    assert (
        saved.error_code == GlobalDeduplicationErrorCode.PROCESSOR_SUBMISSION_UNCERTAIN
    )


def test_transient_submission_releases_lease_for_finite_retry(
    tmp_path: Path,
) -> None:
    session = build_session()
    source, manifest = prepare_manifest(tmp_path)
    adapter = FakeAdapter(
        error=GlobalDeduplicationProcessingError(
            GlobalDeduplicationErrorCode.PROCESSOR_UNAVAILABLE,
            "unavailable",
            transient=True,
        )
    )
    task = build_task(
        session,
        manifest_path=manifest,
        target_path=tmp_path / "published" / "output.json",
    )
    orchestrator = build_orchestrator(
        session,
        input_root=source,
        staging_root=tmp_path / "staging",
        adapter=adapter,
        scheduler=FakeScheduler(),
    )

    with pytest.raises(GlobalDeduplicationProcessingError):
        orchestrator.submit(task.id)

    saved = GlobalDeduplicationTaskRepository(session).get(task.id)
    assert saved is not None
    assert saved.status is GlobalDeduplicationTaskStatus.RUNNING
    assert saved.lease_expires_at is not None
    assert saved.lease_expires_at.replace(tzinfo=UTC) == NOW
    assert saved.error_code == GlobalDeduplicationErrorCode.PROCESSOR_UNAVAILABLE
