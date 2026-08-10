import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlmodel import Session

from app.features.global_deduplication.adapters.datajuicer import (
    DataJuicerJob,
    DataJuicerProgress,
    DataJuicerResult,
    DataJuicerSubmission,
    DataJuicerSubmitRequest,
)
from app.features.global_deduplication.errors import (
    GlobalDeduplicationErrorCode,
    GlobalDeduplicationProcessingError,
)
from app.features.global_deduplication.repository import (
    GlobalDeduplicationTaskRepository,
)
from app.features.global_deduplication.state_machine import (
    GlobalDeduplicationTaskStatus,
)
from tests.features.global_deduplication.test_submit_orchestration import (
    FakeScheduler,
    build_orchestrator,
    build_session,
    build_task,
    prepare_manifest,
)

NOW = datetime(2026, 7, 31, 10, 1, tzinfo=UTC)


@dataclass
class PollingAdapter:
    job: DataJuicerJob
    polls: list[uuid.UUID] = field(default_factory=list)

    def submit(self, request: object) -> object:
        raise AssertionError(f"unexpected submit: {request}")

    def get_job(
        self,
        job_id: uuid.UUID,
        **_expected: object,
    ) -> DataJuicerJob:
        self.polls.append(job_id)
        return self.job


@dataclass
class MissingThenSubmittedAdapter:
    submission: DataJuicerSubmission
    submit_requests: list[DataJuicerSubmitRequest] = field(default_factory=list)

    def submit(self, request: DataJuicerSubmitRequest) -> DataJuicerSubmission:
        self.submit_requests.append(request)
        return self.submission

    def get_job(self, *_args: object, **_kwargs: object) -> DataJuicerJob:
        raise GlobalDeduplicationProcessingError(
            GlobalDeduplicationErrorCode.PROCESSOR_JOB_NOT_FOUND,
            "处理器任务不存在",
        )


def stage_running_task(
    session: Session,
    tmp_path: Path,
) -> tuple[object, Path, uuid.UUID]:
    source, manifest = prepare_manifest(tmp_path)
    adapter = SubmitAdapter()
    task = build_task(
        session,
        manifest_path=manifest,
        target_path=tmp_path / "published" / "result.json",
    )
    scheduler = FakeScheduler()
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
    assert saved.staging_path is not None
    return saved, Path(saved.staging_path), adapter.job_id


@dataclass
class SubmitAdapter:
    job_id: uuid.UUID = field(default_factory=uuid.uuid7)

    def submit(self, request: DataJuicerSubmitRequest) -> DataJuicerSubmission:
        return DataJuicerSubmission(
            job_id=self.job_id,
            request_id=request.request_id,
            profile=request.profile,
            status="queued",
        )


def successful_job(
    task_id: uuid.UUID,
    job_id: uuid.UUID,
    output_path: Path,
    output_sha256: str,
) -> DataJuicerJob:
    return DataJuicerJob(
        job_id=job_id,
        request_id=task_id,
        profile="text_exact_minhash_v1",
        status="succeeded",
        progress=DataJuicerProgress(
            phase="completed",
            total=1,
            processed=1,
            percent=100,
        ),
        result=DataJuicerResult(
            output_path=output_path,
            output_sha256=output_sha256,
        ),
        error=None,
    )


def test_successful_poll_finalizes_in_same_task(tmp_path: Path) -> None:
    session = build_session()
    task, staging_root, job_id = stage_running_task(session, tmp_path)
    output = staging_root / "datajuicer-result.jsonl"
    output.write_text(
        '{"uid":0,"clusterId":null,"representative":true,"method":null}\n',
        encoding="utf-8",
    )
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    adapter = PollingAdapter(successful_job(task.id, job_id, output, digest))
    scheduler = FakeScheduler()
    orchestrator = build_orchestrator(
        session,
        input_root=tmp_path / "input",
        staging_root=tmp_path / "staging",
        adapter=adapter,
        scheduler=scheduler,
        now=NOW,
    )

    orchestrator.poll(task.id)

    saved = GlobalDeduplicationTaskRepository(session).get(task.id)
    assert saved is not None
    assert saved.status is GlobalDeduplicationTaskStatus.SUCCEEDED
    assert saved.processing_phase == "completed"
    assert saved.result_metadata == {
        "total_files": 1,
        "unique_files": 1,
        "moved_duplicates": 0,
        "move_failures": [],
    }


def test_running_job_updates_progress_and_reschedules(tmp_path: Path) -> None:
    session = build_session()
    task, _staging_root, job_id = stage_running_task(session, tmp_path)
    adapter = PollingAdapter(
        DataJuicerJob(
            job_id=job_id,
            request_id=task.id,
            profile="text_exact_minhash_v1",
            status="running",
            progress=DataJuicerProgress(
                phase="minhash_clustering",
                total=10,
                processed=5,
                percent=50,
            ),
            result=None,
            error=None,
        )
    )
    scheduler = FakeScheduler()
    orchestrator = build_orchestrator(
        session,
        input_root=tmp_path / "input",
        staging_root=tmp_path / "staging",
        adapter=adapter,
        scheduler=scheduler,
        now=NOW,
    )

    orchestrator.poll(task.id)

    saved = GlobalDeduplicationTaskRepository(session).get(task.id)
    assert saved is not None
    assert saved.status is GlobalDeduplicationTaskStatus.RUNNING
    assert saved.processing_phase == "deduplicating"
    assert saved.external_status == "running"
    assert saved.poll_lease_expires_at is None
    assert scheduler.polls


def test_processing_deadline_fails_before_external_poll(tmp_path: Path) -> None:
    session = build_session()
    task, staging_root, job_id = stage_running_task(session, tmp_path)
    task.processing_deadline = NOW - timedelta(seconds=1)
    session.add(task)
    session.commit()
    adapter = PollingAdapter(
        successful_job(task.id, job_id, staging_root / "missing.jsonl", "a" * 64)
    )
    orchestrator = build_orchestrator(
        session,
        input_root=tmp_path / "input",
        staging_root=tmp_path / "staging",
        adapter=adapter,
        scheduler=FakeScheduler(),
        now=NOW,
    )

    orchestrator.poll(task.id)

    saved = GlobalDeduplicationTaskRepository(session).get(task.id)
    assert saved is not None
    assert saved.status is GlobalDeduplicationTaskStatus.FAILED
    assert saved.error_code == "PROCESSOR_TIMEOUT"
    assert adapter.polls == []


def test_failed_processor_job_maps_to_stable_error(tmp_path: Path) -> None:
    session = build_session()
    task, _staging_root, job_id = stage_running_task(session, tmp_path)
    adapter = PollingAdapter(
        DataJuicerJob(
            job_id=job_id,
            request_id=task.id,
            profile="text_exact_minhash_v1",
            status="failed",
            progress=DataJuicerProgress(
                phase="failed",
                total=1,
                processed=0,
                percent=0,
            ),
            result=None,
            error=None,
        )
    )
    orchestrator = build_orchestrator(
        session,
        input_root=tmp_path / "input",
        staging_root=tmp_path / "staging",
        adapter=adapter,
        scheduler=FakeScheduler(),
        now=NOW,
    )

    orchestrator.poll(task.id)

    saved = GlobalDeduplicationTaskRepository(session).get(task.id)
    assert saved is not None
    assert saved.status is GlobalDeduplicationTaskStatus.FAILED
    assert saved.error_code == "PROCESSOR_FAILED"


def test_invalid_processor_output_fails_without_publishing(
    tmp_path: Path,
) -> None:
    session = build_session()
    task, staging_root, job_id = stage_running_task(session, tmp_path)
    output = staging_root / "datajuicer-result.jsonl"
    output.write_text(
        '{"uid":999,"clusterId":null,"representative":true,"method":null}\n',
        encoding="utf-8",
    )
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    orchestrator = build_orchestrator(
        session,
        input_root=tmp_path / "input",
        staging_root=tmp_path / "staging",
        adapter=PollingAdapter(successful_job(task.id, job_id, output, digest)),
        scheduler=FakeScheduler(),
        now=NOW,
    )

    orchestrator.poll(task.id)

    saved = GlobalDeduplicationTaskRepository(session).get(task.id)
    assert saved is not None
    assert saved.status is GlobalDeduplicationTaskStatus.FAILED
    assert saved.error_code == "INVALID_PROCESSOR_OUTPUT"
    assert not (tmp_path / "published" / "result.json").exists()


def test_missing_processor_job_is_resubmitted_only_once(tmp_path: Path) -> None:
    session = build_session()
    task, _staging_root, _job_id = stage_running_task(session, tmp_path)
    replacement_job_id = uuid.uuid7()
    adapter = MissingThenSubmittedAdapter(
        DataJuicerSubmission(
            job_id=replacement_job_id,
            request_id=task.id,
            profile="text_exact_minhash_v1",
            status="queued",
        )
    )
    scheduler = FakeScheduler()
    orchestrator = build_orchestrator(
        session,
        input_root=tmp_path / "input",
        staging_root=tmp_path / "staging",
        adapter=adapter,
        scheduler=scheduler,
        now=NOW,
    )

    orchestrator.poll(task.id)

    saved = GlobalDeduplicationTaskRepository(session).get(task.id)
    assert saved is not None
    assert saved.status is GlobalDeduplicationTaskStatus.RUNNING
    assert saved.external_job_id == replacement_job_id
    assert saved.external_progress == {"jobNotFoundResubmitted": True}
    assert len(adapter.submit_requests) == 1
    assert scheduler.polls

    saved.next_poll_at = NOW
    saved.poll_lease_expires_at = None
    session.add(saved)
    session.commit()
    orchestrator.poll(task.id)

    failed = GlobalDeduplicationTaskRepository(session).get(task.id)
    assert failed is not None
    assert failed.status is GlobalDeduplicationTaskStatus.FAILED
    assert failed.error_code == "PROCESSOR_JOB_NOT_FOUND"
    assert len(adapter.submit_requests) == 1
