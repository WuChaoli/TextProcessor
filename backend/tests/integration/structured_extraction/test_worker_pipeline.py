import hashlib
import uuid
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, delete, select

from app.api.deps import get_current_user, get_db
from app.core.config import ExtractionWorkerSettings
from app.core.db import engine
from app.features.structured_extraction.errors import (
    ExtractionErrorCode,
    ExtractionProcessingError,
)
from app.features.structured_extraction.input_resolver import InputResolver
from app.features.structured_extraction.models import (
    ExtractionTask,
    ExtractionTaskStatus,
    ProcessorSlot,
    get_datetime_utc,
)
from app.features.structured_extraction.orchestration import ExtractionOrchestrator
from app.features.structured_extraction.request_policy import RequestPolicy
from app.features.structured_extraction.routes import (
    get_extraction_dispatcher,
    get_request_policy,
)
from app.features.structured_extraction.worker_models import (
    ExternalTaskState,
    ExternalTaskStatus,
    ExternalTaskSubmission,
    ProcessingContext,
    ProcessorArtifact,
    ProcessorName,
)
from app.main import app
from app.models import User


class RecordingDispatcher:
    def __init__(self) -> None:
        self.task_ids: list[uuid.UUID] = []

    def enqueue_submit(self, task_id: uuid.UUID) -> None:
        self.task_ids.append(task_id)


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
        state: ExternalTaskState = ExternalTaskState.SUCCEEDED,
        markdown: str = "# converted\n",
    ) -> None:
        self.state = state
        self.markdown = markdown
        self.submissions: list[Path] = []
        self.polls: list[str] = []
        self.fetches: list[str] = []

    def submit(
        self,
        source: Path,
        context: ProcessingContext,
    ) -> ExternalTaskSubmission:
        del context
        self.submissions.append(source)
        return ExternalTaskSubmission(
            external_task_id="external-task",
            processor_name=ProcessorName.MINERU,
            processor_version="test-processor",
        )

    def get_status(self, external_task_id: str) -> ExternalTaskStatus:
        self.polls.append(external_task_id)
        return ExternalTaskStatus(self.state)

    def fetch_result(
        self,
        external_task_id: str,
        destination: Path,
    ) -> ProcessorArtifact:
        self.fetches.append(external_task_id)
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination.write_text(self.markdown, encoding="utf-8", newline="")
        return ProcessorArtifact(
            markdown_path=destination,
            processor_name=ProcessorName.MINERU,
            processor_version="test-processor",
            profile_name="test-profile",
            profile_sha256="b" * 64,
        )


@dataclass
class ApiContext:
    client: TestClient
    caller_id: uuid.UUID
    input_root: Path
    output_root: Path
    staging_root: Path
    dispatcher: RecordingDispatcher


@pytest.fixture
def api_context(tmp_path: Path) -> Generator[ApiContext]:
    assert engine.dialect.name == "postgresql"
    caller_id = uuid.uuid4()
    caller_email = f"worker-pipeline-{uuid.uuid4()}@example.com"
    caller = User(
        id=caller_id,
        email=caller_email,
        hashed_password="not-used",
    )
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    staging_root = tmp_path / "staging"
    input_root.mkdir()
    output_root.mkdir()
    dispatcher = RecordingDispatcher()
    policy = RequestPolicy(
        input_roots=(input_root,),
        output_roots=(output_root,),
        allowed_http_hosts=("files.internal",),
        allowed_http_cidrs=("10.20.0.0/16",),
        max_input_bytes=1024 * 1024,
        resolver=lambda _host, _port: ("10.20.0.8",),
    )
    with Session(engine) as session:
        session.add(caller)
        session.commit()

    def session_dependency() -> Generator[Session]:
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = session_dependency
    api_caller = User(
        id=caller_id,
        email=caller_email,
        hashed_password="not-used",
    )
    app.dependency_overrides[get_current_user] = lambda: api_caller
    app.dependency_overrides[get_request_policy] = lambda: policy
    app.dependency_overrides[get_extraction_dispatcher] = lambda: dispatcher
    try:
        with TestClient(app) as client:
            yield ApiContext(
                client=client,
                caller_id=caller_id,
                input_root=input_root,
                output_root=output_root,
                staging_root=staging_root,
                dispatcher=dispatcher,
            )
    finally:
        app.dependency_overrides.clear()
        with Session(engine) as session:
            task_ids = select(ExtractionTask.id).where(
                ExtractionTask.caller_id == caller_id
            )
            session.exec(
                delete(ProcessorSlot).where(ProcessorSlot.task_id.in_(task_ids))
            )
            session.exec(
                delete(ExtractionTask).where(ExtractionTask.caller_id == caller_id)
            )
            session.exec(delete(User).where(User.id == caller_id))
            session.commit()


def worker_settings(
    context: ApiContext,
    *,
    production_formats: tuple[str, ...] = ("text", "pdf"),
    max_in_flight: int = 1,
    quarantine_grace_seconds: int = 60,
) -> ExtractionWorkerSettings:
    return ExtractionWorkerSettings(
        staging_root=context.staging_root,
        output_roots=(context.output_root,),
        production_formats=production_formats,
        poll_interval_seconds=1,
        poll_lease_seconds=1,
        processing_deadline_seconds=30,
        mineru_max_in_flight_tasks=max_in_flight,
        slot_quarantine_grace_seconds=quarantine_grace_seconds,
    )


def make_orchestrator(
    context: ApiContext,
    *,
    scheduler: RecordingScheduler | None = None,
    adapter: FakeExternalAdapter | None = None,
    max_in_flight: int = 1,
    quarantine_grace_seconds: int = 60,
) -> ExtractionOrchestrator:
    return ExtractionOrchestrator(
        Session(engine),
        worker_settings=worker_settings(
            context,
            max_in_flight=max_in_flight,
            quarantine_grace_seconds=quarantine_grace_seconds,
        ),
        input_roots=(context.input_root,),
        max_input_bytes=1024 * 1024,
        scheduler=scheduler,
        adapter_factory=(lambda _processor: adapter) if adapter is not None else None,
    )


def create_queued_task(
    context: ApiContext,
    *,
    source: Path,
    target: Path,
    file_id: str | None = None,
) -> ExtractionTask:
    task = ExtractionTask(
        caller_id=context.caller_id,
        session_id=f"worker-pipeline-{uuid.uuid4()}",
        file_id=file_id or uuid.uuid4().hex,
        request_fingerprint="a" * 64,
        file_storage_path=str(source),
        selected_input_type="local",
        target_path=str(target),
        status=ExtractionTaskStatus.QUEUED,
        queued_at=get_datetime_utc(),
    )
    with Session(engine) as session:
        session.add(task)
        session.commit()
        session.refresh(task)
        return task


def task_status(task_id: uuid.UUID) -> ExtractionTask:
    with Session(engine) as session:
        task = session.get(ExtractionTask, task_id)
        assert task is not None
        session.expunge(task)
        return task


def test_api_plain_text_worker_pipeline_preserves_utf8_and_metadata(
    api_context: ApiContext,
) -> None:
    content = "标题\n\n正文\n"
    source = api_context.input_root / "note.txt"
    target = api_context.output_root / "note.md"
    source.write_text(content, encoding="utf-8", newline="")
    response = api_context.client.post(
        "/api/v1/structured-extraction/tasks",
        json={
            "sessionId": "plain-text-e2e",
            "fileId": "note-1",
            "fileStoragePath": str(source),
            "targetPath": str(target),
        },
    )

    assert response.status_code == 202
    task_id = uuid.UUID(response.json()["taskId"])
    assert api_context.dispatcher.task_ids == [task_id]
    worker = make_orchestrator(api_context)
    try:
        worker.submit(task_id)
    finally:
        worker._session.close()  # noqa: SLF001 - integration lifecycle ownership

    result = api_context.client.get(f"/api/v1/structured-extraction/tasks/{task_id}")

    assert result.status_code == 200
    body = result.json()
    assert body["status"] == "succeeded"
    assert target.read_bytes() == content.encode("utf-8")
    assert not target.read_bytes().startswith(b"\xef\xbb\xbf")
    assert body["result"]["processor"] == {
        "name": "plain_text",
        "version": "builtin",
        "profile": "text-pass-through",
        "profileSha256": hashlib.sha256(
            b'{"encodings":["utf-8-sig","gb18030"]}'
        ).hexdigest(),
    }
    assert body["result"]["routing"] == {
        "detectedFormat": "text",
        "reasons": ["fixed_route=text"],
    }
    assert (
        body["result"]["inputSha256"]
        == hashlib.sha256(content.encode("utf-8")).hexdigest()
    )
    assert body["result"]["outputSha256"] == body["result"]["inputSha256"]


def test_two_tasks_for_one_target_have_one_atomic_success(
    api_context: ApiContext,
) -> None:
    source = api_context.input_root / "race.txt"
    source.write_text("同一目标\n", encoding="utf-8")
    target = api_context.output_root / "race.md"
    first = create_queued_task(api_context, source=source, target=target)
    second = create_queued_task(api_context, source=source, target=target)

    def submit(task_id: uuid.UUID) -> None:
        worker = make_orchestrator(api_context)
        try:
            worker.submit(task_id)
        finally:
            worker._session.close()  # noqa: SLF001 - integration lifecycle ownership

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(submit, (first.id, second.id)))

    tasks = (task_status(first.id), task_status(second.id))
    assert sum(task.status is ExtractionTaskStatus.SUCCEEDED for task in tasks) == 1
    failed = next(task for task in tasks if task.status is ExtractionTaskStatus.FAILED)
    assert failed.error_code == ExtractionErrorCode.OUTPUT_CONFLICT
    assert target.read_text(encoding="utf-8") == "同一目标\n"


def test_published_external_result_recovers_after_database_transition_crash(
    api_context: ApiContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = api_context.input_root / "recover.pdf"
    source.write_bytes(b"%PDF-1.7\nplaceholder")
    task = create_queued_task(
        api_context,
        source=source,
        target=api_context.output_root / "recover.md",
    )
    scheduler = RecordingScheduler()
    adapter = FakeExternalAdapter(markdown="# recovered\n")
    worker = make_orchestrator(api_context, scheduler=scheduler, adapter=adapter)
    worker.submit(task.id)
    original_transition = worker._repository.transition  # noqa: SLF001

    def crash_after_publish(
        task_id: uuid.UUID,
        *,
        expected: ExtractionTaskStatus,
        target: ExtractionTaskStatus,
        **fields: object,
    ) -> ExtractionTask:
        if target is ExtractionTaskStatus.SUCCEEDED:
            worker._session.rollback()  # noqa: SLF001
            raise SystemExit("simulate worker crash after publish")
        return original_transition(
            task_id,
            expected=expected,
            target=target,
            **fields,
        )

    monkeypatch.setattr(worker._repository, "transition", crash_after_publish)  # noqa: SLF001
    scheduled = task_status(task.id)
    assert scheduled.next_poll_at is not None
    with pytest.raises(SystemExit, match="after publish"):
        worker.poll(task.id, now=scheduled.next_poll_at + timedelta(seconds=1))
    worker._session.close()  # noqa: SLF001
    assert (api_context.output_root / "recover.md").read_text(
        encoding="utf-8"
    ) == "# recovered\n"

    resumed = make_orchestrator(
        api_context, scheduler=RecordingScheduler(), adapter=adapter
    )
    try:
        resumed.poll(task.id, now=scheduled.next_poll_at + timedelta(seconds=3))
    finally:
        resumed._session.close()  # noqa: SLF001
    recovered = task_status(task.id)
    assert recovered.status is ExtractionTaskStatus.SUCCEEDED
    assert recovered.output_sha256 == hashlib.sha256(b"# recovered\n").hexdigest()
    assert len(adapter.fetches) == 2


def test_duplicate_submit_and_poll_do_not_duplicate_external_work(
    api_context: ApiContext,
) -> None:
    source = api_context.input_root / "idempotent.pdf"
    source.write_bytes(b"%PDF-1.7\nplaceholder")
    task = create_queued_task(
        api_context,
        source=source,
        target=api_context.output_root / "idempotent.md",
    )
    scheduler = RecordingScheduler()
    adapter = FakeExternalAdapter()
    worker = make_orchestrator(api_context, scheduler=scheduler, adapter=adapter)
    try:
        worker.submit(task.id)
        worker.submit(task.id)
        scheduled = task_status(task.id)
        assert scheduled.next_poll_at is not None
        worker.poll(task.id, now=scheduled.next_poll_at + timedelta(seconds=1))
        worker.poll(task.id, now=scheduled.next_poll_at + timedelta(seconds=1))
    finally:
        worker._session.close()  # noqa: SLF001

    assert len(adapter.submissions) == 1
    assert adapter.polls == ["external-task"]
    assert adapter.fetches == ["external-task"]
    assert task_status(task.id).status is ExtractionTaskStatus.SUCCEEDED


def test_recover_reschedules_dropped_poll_and_completes_task(
    api_context: ApiContext,
) -> None:
    source = api_context.input_root / "lost-poll.pdf"
    source.write_bytes(b"%PDF-1.7\nplaceholder")
    task = create_queued_task(
        api_context,
        source=source,
        target=api_context.output_root / "lost-poll.md",
    )
    scheduler = RecordingScheduler()
    adapter = FakeExternalAdapter()
    worker = make_orchestrator(api_context, scheduler=scheduler, adapter=adapter)
    worker.submit(task.id)
    scheduled = task_status(task.id)
    assert scheduled.next_poll_at is not None
    scheduler.poll_calls.clear()
    recovered_count = worker.recover(now=scheduled.next_poll_at + timedelta(seconds=1))
    assert recovered_count >= 1
    assert scheduler.poll_calls == [(task.id, 0)]
    worker.poll(task.id, now=scheduled.next_poll_at + timedelta(seconds=1))
    worker._session.close()  # noqa: SLF001
    assert task_status(task.id).status is ExtractionTaskStatus.SUCCEEDED


@pytest.mark.parametrize(
    ("target_host", "addresses"),
    [
        ("loopback.internal", ("127.0.0.1",)),
        ("linklocal.internal", ("169.254.169.254",)),
        ("metadata.internal", ("169.254.169.254",)),
    ],
)
def test_http_redirect_revalidates_each_hop_against_ssrf_policy(
    tmp_path: Path,
    target_host: str,
    addresses: tuple[str, ...],
) -> None:
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    input_root.mkdir()
    output_root.mkdir()
    policy = RequestPolicy(
        input_roots=(input_root,),
        output_roots=(output_root,),
        allowed_http_hosts=(
            "files.internal",
            "loopback.internal",
            "linklocal.internal",
            "metadata.internal",
        ),
        allowed_http_cidrs=("10.20.0.0/16",),
        max_input_bytes=1024,
        resolver=lambda host, _port: (
            ("10.20.0.8",) if host == "files.internal" else addresses
        ),
    )
    sent_urls: list[str] = []

    def redirect(request: httpx.Request) -> httpx.Response:
        sent_urls.append(str(request.url))
        return httpx.Response(
            302,
            headers={"location": f"http://{target_host}/secret.txt"},
            request=request,
        )

    task = ExtractionTask(
        caller_id=uuid.uuid4(),
        session_id="ssrf",
        file_id="redirect",
        request_fingerprint="a" * 64,
        file_oss_url="http://files.internal/start.txt",
        selected_input_type="remote",
        target_path=str(output_root / "result.md"),
        status=ExtractionTaskStatus.QUEUED,
    )
    resolver = InputResolver(
        input_roots=(input_root,),
        max_input_bytes=1024,
        remote_url_validator=policy.validate_remote_url,
        http_client=httpx.Client(transport=httpx.MockTransport(redirect)),
    )

    with pytest.raises(ExtractionProcessingError) as raised:
        resolver.resolve(task, layout=_layout(tmp_path, task.id))

    assert raised.value.code is ExtractionErrorCode.INPUT_ACCESS_FAILED
    assert sent_urls == ["http://files.internal/start.txt"]


def test_api_rejects_symlink_escape_and_url_credentials(
    api_context: ApiContext,
) -> None:
    private = api_context.input_root.parent / "private"
    private.mkdir()
    secret = private / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    link = api_context.input_root / "escape.txt"
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip("当前环境不允许创建符号链接")
    escaped = api_context.client.post(
        "/api/v1/structured-extraction/tasks",
        json={
            "sessionId": "escape",
            "fileId": "escape",
            "fileStoragePath": str(link),
            "targetPath": str(api_context.output_root / "escape.md"),
        },
    )
    credentials = api_context.client.post(
        "/api/v1/structured-extraction/tasks",
        json={
            "sessionId": "credentials",
            "fileId": "credentials",
            "fileOssUrl": "http://user:pass@files.internal/secret.txt",
            "targetPath": str(api_context.output_root / "credentials.md"),
        },
    )

    assert escaped.status_code == 400
    assert (
        escaped.json()["detail"]["code"] == ExtractionErrorCode.INPUT_PATH_NOT_ALLOWED
    )
    assert credentials.status_code == 400
    assert (
        credentials.json()["detail"]["code"]
        == ExtractionErrorCode.INPUT_URL_NOT_ALLOWED
    )
    assert "user:pass" not in credentials.text


def test_s3_bucket_outside_allowlist_is_rejected_before_access(tmp_path: Path) -> None:
    task = ExtractionTask(
        caller_id=uuid.uuid4(),
        session_id="s3",
        file_id="bucket",
        request_fingerprint="a" * 64,
        file_oss_url="s3://outside-allowed-bucket/input.txt",
        selected_input_type="remote",
        target_path=str(tmp_path / "result.md"),
        status=ExtractionTaskStatus.QUEUED,
    )
    resolver = InputResolver(
        input_roots=(tmp_path,),
        max_input_bytes=1024,
        allowed_s3_buckets=("inside-allowed-bucket",),
    )

    with pytest.raises(ExtractionProcessingError) as raised:
        resolver.resolve(task, layout=_layout(tmp_path, task.id))

    assert raised.value.code is ExtractionErrorCode.INPUT_ACCESS_FAILED


def test_slot_capacity_timeout_quarantine_reap_and_terminal_release(
    api_context: ApiContext,
) -> None:
    source = api_context.input_root / "capacity.pdf"
    source.write_bytes(b"%PDF-1.7\nplaceholder")
    first = create_queued_task(
        api_context,
        source=source,
        target=api_context.output_root / "first.md",
    )
    second = create_queued_task(
        api_context,
        source=source,
        target=api_context.output_root / "second.md",
    )
    scheduler = RecordingScheduler()
    adapter = FakeExternalAdapter(state=ExternalTaskState.PROCESSING)
    worker = make_orchestrator(
        api_context,
        scheduler=scheduler,
        adapter=adapter,
        quarantine_grace_seconds=60,
    )
    worker.submit(first.id)
    worker.submit(second.id)
    waiting = task_status(second.id)
    assert waiting.status is ExtractionTaskStatus.QUEUED
    assert waiting.processing_phase == "waiting_capacity"

    active = task_status(first.id)
    assert active.processing_deadline is not None
    recovered = worker.recover(now=active.processing_deadline + timedelta(seconds=1))
    timed_out = task_status(first.id)
    assert recovered >= 1
    assert timed_out.status is ExtractionTaskStatus.FAILED
    assert timed_out.error_code == ExtractionErrorCode.PROCESSING_TIMEOUT
    with Session(engine) as session:
        slot = session.exec(
            select(ProcessorSlot).where(ProcessorSlot.task_id == first.id)
        ).one()
        assert slot.state == "quarantined"

    worker.recover(now=active.processing_deadline + timedelta(seconds=61))
    with Session(engine) as session:
        assert session.get(ProcessorSlot, slot.id) is None
    worker.submit(second.id)
    adapter.state = ExternalTaskState.SUCCEEDED
    second_scheduled = task_status(second.id)
    assert second_scheduled.next_poll_at is not None
    worker.poll(second.id, now=second_scheduled.next_poll_at + timedelta(seconds=1))
    worker._session.close()  # noqa: SLF001
    assert task_status(second.id).status is ExtractionTaskStatus.SUCCEEDED
    with Session(engine) as session:
        assert (
            session.exec(
                select(ProcessorSlot).where(ProcessorSlot.task_id == second.id)
            ).one_or_none()
            is None
        )


def _layout(tmp_path: Path, task_id: uuid.UUID):
    from app.features.structured_extraction.staging import StagingLayout

    return StagingLayout.for_task(tmp_path / "staging", task_id)
