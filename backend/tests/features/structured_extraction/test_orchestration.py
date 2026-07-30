import uuid
from collections.abc import Callable, Generator
from datetime import timedelta
from pathlib import Path

import pytest
from celery.exceptions import Retry
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.core.config import ExtractionWorkerSettings
from app.features.structured_extraction import celery_tasks
from app.features.structured_extraction.adapters.protocol import (
    ExternalProcessorAdapter,
)
from app.features.structured_extraction.errors import (
    ExtractionErrorCode,
    ExtractionProcessingError,
)
from app.features.structured_extraction.models import (
    ExtractionTask,
    ExtractionTaskStatus,
    ProcessorSlot,
    get_datetime_utc,
)
from app.features.structured_extraction.orchestration import ExtractionOrchestrator
from app.features.structured_extraction.slots import ProcessorSlotRepository
from app.features.structured_extraction.worker_models import (
    ExternalTaskState,
    ExternalTaskStatus,
    ExternalTaskSubmission,
    ProcessingContext,
    ProcessorArtifact,
    ProcessorName,
)
from app.models import User  # noqa: F401


class RecordingScheduler:
    def __init__(self) -> None:
        self.submit_calls: list[tuple[uuid.UUID, int]] = []
        self.poll_calls: list[tuple[uuid.UUID, int]] = []

    def enqueue_submit(self, task_id: uuid.UUID, *, countdown: int) -> None:
        self.submit_calls.append((task_id, countdown))

    def enqueue_poll(self, task_id: uuid.UUID, *, countdown: int) -> None:
        self.poll_calls.append((task_id, countdown))


class FakeExternalAdapter:
    def __init__(
        self,
        *,
        status: ExternalTaskStatus = ExternalTaskStatus(ExternalTaskState.PROCESSING),
        submit_error: ExtractionProcessingError | None = None,
        fetch_error: ExtractionProcessingError | None = None,
        result_markdown: str = "# converted\n",
    ) -> None:
        self.status = status
        self.submit_error = submit_error
        self.fetch_error = fetch_error
        self.result_markdown = result_markdown
        self.submissions: list[Path] = []
        self.status_queries: list[str] = []
        self.result_fetches: list[str] = []

    def submit(
        self,
        source: Path,
        context: ProcessingContext,
    ) -> ExternalTaskSubmission:
        del context
        self.submissions.append(source)
        if self.submit_error is not None:
            raise self.submit_error
        return ExternalTaskSubmission(
            external_task_id="mineru-1",
            processor_name=ProcessorName.MINERU,
            processor_version="2026.07",
        )

    def get_status(self, external_task_id: str) -> ExternalTaskStatus:
        self.status_queries.append(external_task_id)
        return self.status

    def fetch_result(
        self,
        external_task_id: str,
        destination: Path,
    ) -> ProcessorArtifact:
        self.result_fetches.append(external_task_id)
        if self.fetch_error is not None:
            raise self.fetch_error
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination.write_text(self.result_markdown, encoding="utf-8")
        return ProcessorArtifact(
            markdown_path=destination,
            processor_name=ProcessorName.MINERU,
            processor_version="2026.07",
            profile_name="mineru-default",
            profile_sha256="a" * 64,
        )


class ForbiddenSlots:
    def acquire(self, **_kwargs: object) -> None:
        raise AssertionError("plain text route must not acquire a processor slot")


class RetryRequested(Exception):
    pass


class RetryRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[ExtractionProcessingError, int]] = []

    def retry(
        self,
        *,
        exc: ExtractionProcessingError,
        countdown: int,
    ) -> None:
        self.calls.append((exc, countdown))
        raise RetryRequested


@pytest.fixture
def session() -> Generator[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as value:
        yield value


def make_task(
    *,
    source: Path,
    target: Path,
    status: ExtractionTaskStatus = ExtractionTaskStatus.QUEUED,
    **fields: object,
) -> ExtractionTask:
    return ExtractionTask(
        caller_id=uuid.uuid4(),
        session_id="session-1",
        file_id=f"file-{uuid.uuid4()}",
        request_fingerprint="a" * 64,
        file_storage_path=str(source),
        selected_input_type="local",
        target_path=str(target),
        status=status,
        **fields,
    )


def make_orchestrator(
    session: Session,
    *,
    input_root: Path,
    output_root: Path,
    staging_root: Path,
    scheduler: RecordingScheduler,
    adapter_factory: Callable[[ProcessorName], ExternalProcessorAdapter],
    slots: ProcessorSlotRepository | ForbiddenSlots | None = None,
    production_formats: tuple[str, ...] = ("pdf", "text"),
) -> ExtractionOrchestrator:
    return ExtractionOrchestrator(
        session,
        worker_settings=ExtractionWorkerSettings(
            staging_root=staging_root,
            output_roots=(output_root,),
            production_formats=production_formats,
            poll_interval_seconds=7,
            processing_deadline_seconds=60,
            poll_lease_seconds=20,
            mineru_max_in_flight_tasks=1,
        ),
        input_roots=(input_root,),
        max_input_bytes=1024 * 1024,
        scheduler=scheduler,
        adapter_factory=adapter_factory,
        slots=slots,
    )


def test_submit_processes_plain_text_without_external_adapter_or_slot(
    session: Session,
    tmp_path: Path,
) -> None:
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    staging_root = tmp_path / "staging"
    input_root.mkdir()
    output_root.mkdir()
    source = input_root / "note.txt"
    source.write_text("标题\n\n正文\n", encoding="utf-8")
    target = output_root / "note.md"
    task = make_task(source=source, target=target)
    session.add(task)
    session.commit()
    scheduler = RecordingScheduler()

    def fail_adapter(_processor: ProcessorName) -> ExternalProcessorAdapter:
        raise AssertionError("plain text route must not create an external adapter")

    make_orchestrator(
        session,
        input_root=input_root,
        output_root=output_root,
        staging_root=staging_root,
        scheduler=scheduler,
        adapter_factory=fail_adapter,
        slots=ForbiddenSlots(),
    ).submit(task.id)

    session.refresh(task)
    assert task.status is ExtractionTaskStatus.SUCCEEDED
    assert task.started_at is not None
    assert task.lease_expires_at is None
    assert task.processor_name == ProcessorName.PLAIN_TEXT
    assert target.read_text(encoding="utf-8") == "标题\n\n正文\n"
    assert scheduler.submit_calls == []
    assert scheduler.poll_calls == []


def test_submit_waits_and_reschedules_when_external_capacity_is_exhausted(
    session: Session,
    tmp_path: Path,
) -> None:
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    staging_root = tmp_path / "staging"
    input_root.mkdir()
    output_root.mkdir()
    source = input_root / "report.pdf"
    source.write_bytes(b"%PDF-1.7\nplaceholder")
    holder = make_task(
        source=source,
        target=output_root / "holder.md",
        status=ExtractionTaskStatus.RUNNING,
    )
    task = make_task(source=source, target=output_root / "report.md")
    session.add_all((holder, task))
    session.commit()
    slots = ProcessorSlotRepository(session)
    assert (
        slots.acquire(
            task_id=holder.id,
            processor_name=ProcessorName.MINERU,
            max_in_flight=1,
            lease_duration=timedelta(seconds=60),
        )
        is not None
    )
    scheduler = RecordingScheduler()
    adapter = FakeExternalAdapter()

    make_orchestrator(
        session,
        input_root=input_root,
        output_root=output_root,
        staging_root=staging_root,
        scheduler=scheduler,
        adapter_factory=lambda _processor: adapter,
        slots=slots,
    ).submit(task.id)

    session.refresh(task)
    assert task.status is ExtractionTaskStatus.QUEUED
    assert task.processing_phase == "waiting_capacity"
    assert task.processor_name == ProcessorName.MINERU
    assert adapter.submissions == []
    assert scheduler.submit_calls == [(task.id, 7)]
    assert scheduler.poll_calls == []


def test_submit_claims_capacity_persists_external_id_and_schedules_poll(
    session: Session,
    tmp_path: Path,
) -> None:
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    staging_root = tmp_path / "staging"
    input_root.mkdir()
    output_root.mkdir()
    source = input_root / "report.pdf"
    source.write_bytes(b"%PDF-1.7\nplaceholder")
    task = make_task(source=source, target=output_root / "report.md")
    session.add(task)
    session.commit()
    scheduler = RecordingScheduler()
    adapter = FakeExternalAdapter()

    make_orchestrator(
        session,
        input_root=input_root,
        output_root=output_root,
        staging_root=staging_root,
        scheduler=scheduler,
        adapter_factory=lambda _processor: adapter,
    ).submit(task.id)

    session.refresh(task)
    slot = session.exec(
        select(ProcessorSlot).where(ProcessorSlot.task_id == task.id)
    ).one()
    assert task.status is ExtractionTaskStatus.RUNNING
    assert task.external_task_id == "mineru-1"
    assert task.processing_phase == "submitted"
    assert task.processing_deadline is not None
    assert slot.processor_name == ProcessorName.MINERU
    assert scheduler.poll_calls == [(task.id, 7)]


@pytest.mark.parametrize("status_code", [429, 503])
def test_submit_retries_explicit_safe_http_transient_without_extra_slot(
    session: Session,
    tmp_path: Path,
    status_code: int,
) -> None:
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    staging_root = tmp_path / "staging"
    input_root.mkdir()
    output_root.mkdir()
    source = input_root / "report.pdf"
    source.write_bytes(b"%PDF-1.7\nplaceholder")
    task = make_task(source=source, target=output_root / "report.md")
    session.add(task)
    session.commit()
    scheduler = RecordingScheduler()
    adapter = FakeExternalAdapter(
        submit_error=ExtractionProcessingError(
            ExtractionErrorCode.PROCESSING_FAILED,
            f"processor returned HTTP {status_code}",
            transient=True,
        )
    )
    orchestrator = make_orchestrator(
        session,
        input_root=input_root,
        output_root=output_root,
        staging_root=staging_root,
        scheduler=scheduler,
        adapter_factory=lambda _processor: adapter,
    )

    with pytest.raises(ExtractionProcessingError) as first_raised:
        orchestrator.submit(task.id)
    with pytest.raises(ExtractionProcessingError):
        orchestrator.submit(task.id)

    session.refresh(task)
    slots = session.exec(
        select(ProcessorSlot).where(ProcessorSlot.task_id == task.id)
    ).all()
    assert first_raised.value.transient is True
    assert task.status is ExtractionTaskStatus.RUNNING
    assert task.processing_phase == "submitting"
    assert task.external_task_id is None
    assert len(adapter.submissions) == 2
    assert len(slots) == 1
    assert slots[0].state == "active"
    assert scheduler.poll_calls == []


def test_poll_processing_schedules_one_follow_up_without_blocking(
    session: Session,
    tmp_path: Path,
) -> None:
    task, orchestrator, scheduler, adapter = make_running_external_task(
        session,
        tmp_path,
        status=ExternalTaskStatus(ExternalTaskState.PROCESSING),
    )

    orchestrator.poll(task.id)

    session.refresh(task)
    assert adapter.status_queries == ["mineru-1"]
    assert task.status is ExtractionTaskStatus.RUNNING
    assert task.processing_phase == "submitted"
    assert task.poll_lease_expires_at is None
    assert scheduler.poll_calls == [(task.id, 7)]


def test_poll_success_fetches_normalizes_publishes_and_releases_slot(
    session: Session,
    tmp_path: Path,
) -> None:
    task, orchestrator, scheduler, adapter = make_running_external_task(
        session,
        tmp_path,
        status=ExternalTaskStatus(ExternalTaskState.SUCCEEDED),
        result_markdown="# 标题\n\n![图片](https://example.invalid/image.png)\n\n正文\n",
    )

    orchestrator.poll(task.id)

    session.refresh(task)
    assert task.status is ExtractionTaskStatus.SUCCEEDED
    assert adapter.status_queries == ["mineru-1"]
    assert adapter.result_fetches == ["mineru-1"]
    assert "![" not in Path(task.target_path).read_text(encoding="utf-8")
    assert (
        session.exec(
            select(ProcessorSlot).where(ProcessorSlot.task_id == task.id)
        ).one_or_none()
        is None
    )
    assert scheduler.poll_calls == []


def test_poll_failure_marks_task_failed_and_releases_slot(
    session: Session,
    tmp_path: Path,
) -> None:
    task, orchestrator, scheduler, _adapter = make_running_external_task(
        session,
        tmp_path,
        status=ExternalTaskStatus(
            ExternalTaskState.FAILED,
            safe_error_code=ExtractionErrorCode.PROCESSING_FAILED,
            safe_error_message="processor failed",
        ),
    )

    orchestrator.poll(task.id)

    session.refresh(task)
    assert task.status is ExtractionTaskStatus.FAILED
    assert task.error_code == ExtractionErrorCode.PROCESSING_FAILED
    assert (
        session.exec(
            select(ProcessorSlot).where(ProcessorSlot.task_id == task.id)
        ).one_or_none()
        is None
    )
    assert scheduler.poll_calls == []


def test_poll_transient_result_fetch_clears_lease_for_celery_retry(
    session: Session,
    tmp_path: Path,
) -> None:
    task, orchestrator, _scheduler, adapter = make_running_external_task(
        session,
        tmp_path,
        status=ExternalTaskStatus(ExternalTaskState.SUCCEEDED),
    )
    adapter.fetch_error = ExtractionProcessingError(
        ExtractionErrorCode.PROCESSING_FAILED,
        "temporary result download failure",
        transient=True,
    )

    with pytest.raises(ExtractionProcessingError) as raised:
        orchestrator.poll(task.id)

    session.refresh(task)
    assert raised.value.transient is True
    assert task.status is ExtractionTaskStatus.RUNNING
    assert task.processing_phase == "submitted"
    assert task.poll_lease_expires_at is None
    assert (
        session.exec(
            select(ProcessorSlot).where(ProcessorSlot.task_id == task.id)
        ).one_or_none()
        is None
    )


def test_poll_local_output_error_fails_without_retrying_poll_task(
    session: Session,
    tmp_path: Path,
) -> None:
    task, orchestrator, _scheduler, adapter = make_running_external_task(
        session,
        tmp_path,
        status=ExternalTaskStatus(ExternalTaskState.SUCCEEDED),
    )
    adapter.fetch_error = ExtractionProcessingError(
        ExtractionErrorCode.OUTPUT_WRITE_FAILED,
        "cannot write local staging result",
        transient=True,
    )

    orchestrator.poll(task.id)

    session.refresh(task)
    assert task.status is ExtractionTaskStatus.FAILED
    assert task.error_code == ExtractionErrorCode.OUTPUT_WRITE_FAILED
    assert task.next_poll_at is None
    assert task.poll_lease_expires_at is None


def test_uncertain_submission_is_not_automatically_resubmitted(
    session: Session,
    tmp_path: Path,
) -> None:
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    staging_root = tmp_path / "staging"
    input_root.mkdir()
    output_root.mkdir()
    source = input_root / "report.pdf"
    source.write_bytes(b"%PDF-1.7\nplaceholder")
    task = make_task(source=source, target=output_root / "report.md")
    session.add(task)
    session.commit()
    scheduler = RecordingScheduler()
    adapter = FakeExternalAdapter(
        submit_error=ExtractionProcessingError(
            ExtractionErrorCode.PROCESSOR_SUBMISSION_UNCERTAIN,
            "submission uncertain",
        )
    )
    orchestrator = make_orchestrator(
        session,
        input_root=input_root,
        output_root=output_root,
        staging_root=staging_root,
        scheduler=scheduler,
        adapter_factory=lambda _processor: adapter,
    )

    orchestrator.submit(task.id)
    orchestrator.submit(task.id)

    session.refresh(task)
    slot = session.exec(
        select(ProcessorSlot).where(ProcessorSlot.task_id == task.id)
    ).one()
    assert task.status is ExtractionTaskStatus.FAILED
    assert task.error_code == ExtractionErrorCode.PROCESSOR_SUBMISSION_UNCERTAIN
    assert len(adapter.submissions) == 1
    assert slot.state == "quarantined"
    assert scheduler.submit_calls == []


def test_duplicate_submit_delivery_does_not_submit_external_task_twice(
    session: Session,
    tmp_path: Path,
) -> None:
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    staging_root = tmp_path / "staging"
    input_root.mkdir()
    output_root.mkdir()
    source = input_root / "report.pdf"
    source.write_bytes(b"%PDF-1.7\nplaceholder")
    task = make_task(source=source, target=output_root / "report.md")
    session.add(task)
    session.commit()
    scheduler = RecordingScheduler()
    adapter = FakeExternalAdapter()
    orchestrator = make_orchestrator(
        session,
        input_root=input_root,
        output_root=output_root,
        staging_root=staging_root,
        scheduler=scheduler,
        adapter_factory=lambda _processor: adapter,
    )

    orchestrator.submit(task.id)
    orchestrator.submit(task.id)

    assert len(adapter.submissions) == 1
    assert scheduler.poll_calls == [(task.id, 7)]


@pytest.mark.parametrize("status_code", [429, 503])
def test_submit_task_uses_bounded_retry_for_safe_http_transient(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    error = ExtractionProcessingError(
        ExtractionErrorCode.PROCESSING_FAILED,
        f"processor returned HTTP {status_code}",
        transient=True,
    )

    def fail_submit(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(celery_tasks, "handle_submit_task", fail_submit)
    task = celery_tasks.submit_extraction_task._get_current_object()
    task.push_request(
        retries=task.max_retries - 1,
        called_directly=False,
        is_eager=True,
    )
    try:
        with pytest.raises(Retry) as raised:
            task.run(
                str(uuid.uuid4()),
                "structured_extraction",
                1,
            )
    finally:
        task.pop_request()

    assert raised.value.when == 5
    assert task.max_retries == 3


def test_submit_retry_exhaustion_fails_and_quarantines_slot(
    session: Session,
    tmp_path: Path,
) -> None:
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    staging_root = tmp_path / "staging"
    input_root.mkdir()
    output_root.mkdir()
    source = input_root / "report.pdf"
    source.write_bytes(b"%PDF-1.7\nplaceholder")
    now = get_datetime_utc()
    task = make_task(
        source=source,
        target=output_root / "report.md",
        status=ExtractionTaskStatus.RUNNING,
        processor_name=ProcessorName.MINERU,
        processing_phase="submitting",
        processing_deadline=now + timedelta(seconds=60),
    )
    session.add(task)
    session.commit()
    slots = ProcessorSlotRepository(session)
    assert (
        slots.acquire(
            task_id=task.id,
            processor_name=ProcessorName.MINERU,
            max_in_flight=1,
            lease_duration=timedelta(seconds=60),
        )
        is not None
    )
    error = ExtractionProcessingError(
        ExtractionErrorCode.PROCESSING_FAILED,
        "processor returned HTTP 503",
        transient=True,
    )

    celery_tasks.handle_submit_retry_exhausted(
        session,
        task_id=str(task.id),
        task_type="structured_extraction",
        schema_version=1,
        error=error,
        worker_settings=ExtractionWorkerSettings(
            staging_root=staging_root,
            output_roots=(output_root,),
            production_formats=("pdf",),
        ),
        input_roots=(input_root,),
        max_input_bytes=1024 * 1024,
    )

    session.refresh(task)
    slot = session.exec(
        select(ProcessorSlot).where(ProcessorSlot.task_id == task.id)
    ).one()
    assert task.status is ExtractionTaskStatus.FAILED
    assert task.error_code == ExtractionErrorCode.PROCESSOR_SUBMISSION_UNCERTAIN
    assert task.processing_phase is None
    assert task.external_task_id is None
    assert task.next_poll_at is None
    assert slot.state == "quarantined"


def test_submit_task_exhaustion_does_not_schedule_another_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = ExtractionProcessingError(
        ExtractionErrorCode.PROCESSING_FAILED,
        "processor returned HTTP 503",
        transient=True,
    )
    exhausted_calls: list[tuple[str, str, int, ExtractionProcessingError]] = []

    def fail_submit(*_args: object, **_kwargs: object) -> None:
        raise error

    def record_exhaustion(
        _session: Session,
        *,
        task_id: str,
        task_type: str,
        schema_version: int,
        error: ExtractionProcessingError,
    ) -> None:
        exhausted_calls.append((task_id, task_type, schema_version, error))

    def unexpected_retry(*_args: object, **_kwargs: object) -> None:
        pytest.fail("submit retry exhaustion must not schedule another retry")

    monkeypatch.setattr(celery_tasks, "handle_submit_task", fail_submit)
    monkeypatch.setattr(
        celery_tasks,
        "handle_submit_retry_exhausted",
        record_exhaustion,
    )
    task = celery_tasks.submit_extraction_task._get_current_object()
    monkeypatch.setattr(task, "retry", unexpected_retry)
    task_id = str(uuid.uuid4())
    task.push_request(
        retries=task.max_retries,
        called_directly=False,
        is_eager=True,
    )
    try:
        task.run(task_id, "structured_extraction", 1)
    finally:
        task.pop_request()

    assert exhausted_calls == [(task_id, "structured_extraction", 1, error)]


def test_poll_task_does_not_retry_transient_local_output_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = ExtractionProcessingError(
        ExtractionErrorCode.OUTPUT_WRITE_FAILED,
        "cannot write local output",
        transient=True,
    )

    def fail_poll(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(celery_tasks, "handle_poll_task", fail_poll)
    retry = RetryRecorder()

    with pytest.raises(ExtractionProcessingError) as raised:
        celery_tasks.poll_extraction_task.run.__func__(
            retry,
            str(uuid.uuid4()),
            "structured_extraction",
            1,
        )

    assert raised.value is error
    assert retry.calls == []


def make_running_external_task(
    session: Session,
    tmp_path: Path,
    *,
    status: ExternalTaskStatus,
    result_markdown: str = "# converted\n",
) -> tuple[
    ExtractionTask,
    ExtractionOrchestrator,
    RecordingScheduler,
    FakeExternalAdapter,
]:
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    staging_root = tmp_path / "staging"
    input_root.mkdir()
    output_root.mkdir()
    source = input_root / "report.pdf"
    source.write_bytes(b"%PDF-1.7\nplaceholder")
    now = get_datetime_utc()
    task = make_task(
        source=source,
        target=output_root / "report.md",
        status=ExtractionTaskStatus.RUNNING,
        processor_name=ProcessorName.MINERU,
        external_task_id="mineru-1",
        processing_phase="submitted",
        next_poll_at=now - timedelta(seconds=1),
        processing_deadline=now + timedelta(seconds=60),
    )
    session.add(task)
    session.commit()
    slots = ProcessorSlotRepository(session)
    assert (
        slots.acquire(
            task_id=task.id,
            processor_name=ProcessorName.MINERU,
            max_in_flight=1,
            lease_duration=timedelta(seconds=60),
        )
        is not None
    )
    scheduler = RecordingScheduler()
    adapter = FakeExternalAdapter(status=status, result_markdown=result_markdown)
    orchestrator = make_orchestrator(
        session,
        input_root=input_root,
        output_root=output_root,
        staging_root=staging_root,
        scheduler=scheduler,
        adapter_factory=lambda _processor: adapter,
        slots=slots,
    )
    return task, orchestrator, scheduler, adapter
