import hashlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, cast

from app.core.config import GlobalDeduplicationWorkerSettings
from app.features.global_deduplication.adapters.datajuicer import (
    DataJuicerAdapter,
    DataJuicerJob,
    DataJuicerProfile,
    DataJuicerSubmission,
    DataJuicerSubmitRequest,
)
from app.features.global_deduplication.errors import (
    GlobalDeduplicationErrorCode,
    GlobalDeduplicationProcessingError,
)
from app.features.global_deduplication.input_reader import (
    BoundedUriReader,
    load_documents,
    load_manifest_bytes,
)
from app.features.global_deduplication.publisher import FinalResultPublisher
from app.features.global_deduplication.repository import (
    ConditionalGlobalDeduplicationUpdateFailed,
    GlobalDeduplicationTaskRepository,
)
from app.features.global_deduplication.result_mapper import (
    load_mapping,
    map_business_result,
    validate_processor_output,
)
from app.features.global_deduplication.staging import (
    GlobalDeduplicationStaging,
    GlobalDeduplicationStagingLayout,
)
from app.features.global_deduplication.state_machine import (
    GlobalDeduplicationTaskStatus,
)
from app.features.global_deduplication.task_models import GlobalDeduplicationTask


class DataJuicerSubmitter(Protocol):
    def submit(
        self,
        request: DataJuicerSubmitRequest,
    ) -> DataJuicerSubmission: ...


class GlobalDeduplicationScheduler(Protocol):
    def enqueue_submit(self, task_id: uuid.UUID, *, countdown: int) -> None: ...

    def enqueue_poll(self, task_id: uuid.UUID, *, countdown: int) -> None: ...


class DataJuicerPoller(Protocol):
    def get_job(
        self,
        job_id: uuid.UUID,
        *,
        expected_request_id: uuid.UUID,
        expected_profile: DataJuicerProfile,
        expected_output_path: Path,
    ) -> DataJuicerJob: ...


@dataclass(frozen=True, slots=True)
class RecoverySummary:
    submit_dispatched: int
    poll_dispatched: int


class GlobalDeduplicationOrchestrator:
    def __init__(
        self,
        *,
        repository: GlobalDeduplicationTaskRepository,
        reader: BoundedUriReader,
        staging: GlobalDeduplicationStaging,
        adapter: DataJuicerSubmitter | DataJuicerAdapter,
        scheduler: GlobalDeduplicationScheduler,
        settings: GlobalDeduplicationWorkerSettings,
        now: Callable[[], datetime],
        publisher: FinalResultPublisher | None = None,
    ) -> None:
        self._repository = repository
        self._reader = reader
        self._staging = staging
        self._adapter = adapter
        self._scheduler = scheduler
        self._settings = settings
        self._now = now
        self._publisher = publisher or FinalResultPublisher()

    def submit(self, task_id: uuid.UUID) -> None:
        now = self._now()
        task = self._repository.acquire_submit(
            task_id,
            now=now,
            lease_seconds=self._settings.submit_lease_seconds,
        )
        if task is None:
            return
        try:
            target = self._validated_target(task.target_path)
            if target.exists():
                raise GlobalDeduplicationProcessingError(
                    GlobalDeduplicationErrorCode.OUTPUT_CONFLICT,
                    "目标结果文件已存在",
                )
            manifest_content = self._reader.read_manifest(
                task.input_json_path,
                max_bytes=self._settings.max_manifest_bytes,
            )
            references = load_manifest_bytes(
                manifest_content,
                max_documents=self._settings.max_documents,
            )
            self._repository.update_running(
                task.id,
                processing_phase="loading_documents",
                progress_total=len(references),
                progress_processed=0,
                progress_percent=10,
                updated_at=self._now(),
            )
            documents = load_documents(
                references,
                reader=self._reader,
                max_document_bytes=self._settings.max_document_bytes,
                max_total_bytes=self._settings.max_total_bytes,
            )
            prepared = self._staging.prepare(
                task.id,
                documents,
                profile=self._settings.datajuicer_profile,
            )
            if not self._repository.save_prepared_input(
                task.id,
                staging_path=str(prepared.layout.root),
                input_manifest_sha256=hashlib.sha256(
                    manifest_content
                ).hexdigest(),
                input_jsonl_sha256=prepared.input_jsonl_sha256,
                mapping_sha256=prepared.mapping_sha256,
                progress_total=len(documents),
            ):
                return
            submission = self._adapter.submit(
                DataJuicerSubmitRequest(
                    request_id=task.id,
                    profile=self._settings.datajuicer_profile,
                    input_path=prepared.layout.input_jsonl,
                    output_path=prepared.layout.datajuicer_result,
                )
            )
            submitted_at = self._now()
            if not self._repository.save_external_job(
                task.id,
                external_job_id=submission.job_id,
                external_profile=submission.profile,
                next_poll_at=submitted_at
                + timedelta(
                    seconds=(
                        self._settings.datajuicer_poll_initial_delay_seconds
                    )
                ),
                processing_deadline=submitted_at
                + timedelta(
                    seconds=self._settings.datajuicer_processing_timeout_seconds
                ),
            ):
                return
            self._scheduler.enqueue_poll(
                task.id,
                countdown=self._settings.datajuicer_poll_initial_delay_seconds,
            )
        except GlobalDeduplicationProcessingError as error:
            if (
                error.code
                is GlobalDeduplicationErrorCode.PROCESSOR_SUBMISSION_UNCERTAIN
            ):
                self._repository.mark_submission_uncertain(
                    task.id,
                    now=self._now(),
                    error_code=error.code,
                    error_message=error.safe_message,
                )
                return
            if error.transient:
                self._repository.mark_submission_uncertain(
                    task.id,
                    now=self._now(),
                    error_code=error.code,
                    error_message=error.safe_message,
                )
                raise
            self._fail(task, error)
        except OSError:
            self._fail(
                task,
                GlobalDeduplicationProcessingError(
                    GlobalDeduplicationErrorCode.INTERNAL_ERROR,
                    "全局去重输入准备失败",
                ),
            )

    def poll(self, task_id: uuid.UUID) -> None:
        now = self._now()
        task = self._repository.acquire_poll(
            task_id,
            now=now,
            lease_seconds=self._settings.poll_lease_seconds,
        )
        if task is None:
            return
        if self._deadline_expired(task, now):
            self._fail(
                task,
                GlobalDeduplicationProcessingError(
                    GlobalDeduplicationErrorCode.PROCESSOR_TIMEOUT,
                    "全局去重处理超时",
                ),
            )
            return
        if (
            task.external_job_id is None
            or task.external_profile != self._settings.datajuicer_profile
            or task.staging_path is None
        ):
            self._fail(task, self._internal_error())
            return
        layout = GlobalDeduplicationStagingLayout.for_task(
            self._settings.staging_root,
            task.id,
        )
        try:
            job = cast(DataJuicerPoller, self._adapter).get_job(
                task.external_job_id,
                expected_request_id=task.id,
                expected_profile=self._settings.datajuicer_profile,
                expected_output_path=layout.datajuicer_result,
            )
            if job.status in {"pending", "queued", "running"}:
                self._schedule_next_poll(task, job, now)
                return
            if job.status == "failed":
                self._fail(
                    task,
                    GlobalDeduplicationProcessingError(
                        GlobalDeduplicationErrorCode.PROCESSOR_FAILED,
                        "处理器任务执行失败",
                    ),
                )
                return
            if job.status == "cancelled":
                self._fail(
                    task,
                    GlobalDeduplicationProcessingError(
                        GlobalDeduplicationErrorCode.PROCESSOR_CANCELLED,
                        "处理器任务已取消",
                    ),
                )
                return
            self._finalize(task, layout, job)
        except GlobalDeduplicationProcessingError as error:
            if (
                error.code
                is GlobalDeduplicationErrorCode.PROCESSOR_JOB_NOT_FOUND
                and not self._job_not_found_was_resubmitted(task)
            ):
                try:
                    self._resubmit_missing_job(task, layout, now)
                except GlobalDeduplicationProcessingError as resubmit_error:
                    if resubmit_error.transient:
                        self._schedule_poll_retry(task, now)
                    else:
                        self._fail(task, resubmit_error)
                return
            if error.transient:
                self._schedule_poll_retry(task, now)
                return
            self._fail(task, error)

    def recover(self) -> RecoverySummary:
        now = self._now()
        submits = self._repository.list_recoverable_submissions(
            now=now,
            limit=self._settings.recovery_batch_size,
        )
        polls = self._repository.list_due_polls(
            now=now,
            limit=self._settings.recovery_batch_size,
        )
        submit_count = 0
        poll_count = 0
        for task in submits:
            self._scheduler.enqueue_submit(task.id, countdown=0)
            submit_count += 1
        for task in polls:
            self._scheduler.enqueue_poll(task.id, countdown=0)
            poll_count += 1
        return RecoverySummary(
            submit_dispatched=submit_count,
            poll_dispatched=poll_count,
        )

    def fail_exhausted_submission_retry(
        self,
        task_id: uuid.UUID,
        error: GlobalDeduplicationProcessingError,
    ) -> None:
        task = self._repository.get(task_id)
        if task is None or task.status is not GlobalDeduplicationTaskStatus.RUNNING:
            return
        self._fail(task, error)

    def _schedule_next_poll(
        self,
        task: GlobalDeduplicationTask,
        job: DataJuicerJob,
        now: datetime,
    ) -> None:
        previous_count = 0
        if task.external_progress is not None:
            value = task.external_progress.get("pollCount", 0)
            if isinstance(value, int) and not isinstance(value, bool):
                previous_count = value
        poll_count = previous_count + 1
        delay = min(
            self._settings.datajuicer_poll_initial_delay_seconds
            * (2 ** min(poll_count - 1, 20)),
            self._settings.datajuicer_poll_max_delay_seconds,
        )
        updated = self._repository.update_running(
            task.id,
            external_status=job.status,
            external_progress={
                "phase": job.progress.phase,
                "total": job.progress.total,
                "processed": job.progress.processed,
                "percent": job.progress.percent,
                "pollCount": poll_count,
            },
            processing_phase="deduplicating",
            progress_percent=40 + (job.progress.percent * 50 // 100),
            next_poll_at=now + timedelta(seconds=delay),
            poll_lease_expires_at=None,
            updated_at=now,
        )
        if updated:
            self._scheduler.enqueue_poll(task.id, countdown=delay)

    def _schedule_poll_retry(
        self,
        task: GlobalDeduplicationTask,
        now: datetime,
    ) -> None:
        delay = self._settings.datajuicer_poll_initial_delay_seconds
        if self._repository.update_running(
            task.id,
            next_poll_at=now + timedelta(seconds=delay),
            poll_lease_expires_at=None,
            updated_at=now,
        ):
            self._scheduler.enqueue_poll(task.id, countdown=delay)

    def _resubmit_missing_job(
        self,
        task: GlobalDeduplicationTask,
        layout: GlobalDeduplicationStagingLayout,
        now: datetime,
    ) -> None:
        submission = self._adapter.submit(
            DataJuicerSubmitRequest(
                request_id=task.id,
                profile=self._settings.datajuicer_profile,
                input_path=layout.input_jsonl,
                output_path=layout.datajuicer_result,
            )
        )
        delay = self._settings.datajuicer_poll_initial_delay_seconds
        if self._repository.update_running(
            task.id,
            external_job_id=submission.job_id,
            external_profile=submission.profile,
            external_status=submission.status,
            external_progress={"jobNotFoundResubmitted": True},
            next_poll_at=now + timedelta(seconds=delay),
            poll_lease_expires_at=None,
            updated_at=now,
        ):
            self._scheduler.enqueue_poll(task.id, countdown=delay)

    def _finalize(
        self,
        task: GlobalDeduplicationTask,
        layout: GlobalDeduplicationStagingLayout,
        job: DataJuicerJob,
    ) -> None:
        if job.result is None:
            raise self._internal_error()
        mapping = load_mapping(layout.mapping_json, expected_task_id=task.id)
        decisions = validate_processor_output(
            layout.datajuicer_result,
            expected_uids={document.uid for document in mapping},
            expected_sha256=job.result.output_sha256,
        )
        business_result = map_business_result(task.id, mapping, decisions)
        prepared = self._publisher.prepare(
            business_result,
            layout.final_result,
        )
        if not self._repository.save_prepared_output(
            task.id,
            external_output_sha256=job.result.output_sha256,
            prepared_output_sha256=prepared.sha256,
        ):
            return
        published = self._publisher.publish(
            prepared,
            self._validated_target(task.target_path),
            allow_recovery=True,
        )
        self._staging.update_result_manifest(
            layout,
            datajuicer_result_sha256=job.result.output_sha256,
            final_result_sha256=published.sha256,
        )
        finished = self._now()
        self._repository.transition(
            task.id,
            expected=GlobalDeduplicationTaskStatus.RUNNING,
            target=GlobalDeduplicationTaskStatus.SUCCEEDED,
            output_sha256=published.sha256,
            external_output_sha256=job.result.output_sha256,
            published_at=finished,
            finished_at=finished,
            lease_expires_at=None,
            poll_lease_expires_at=None,
            next_poll_at=None,
            processing_phase="completed",
            progress_processed=task.progress_total or len(mapping),
            progress_percent=100,
            error_code=None,
            error_message=None,
            result_metadata={
                "target_path": task.target_path,
                "output_size_bytes": published.size_bytes,
            },
            updated_at=finished,
        )

    def _validated_target(self, value: str) -> Path:
        target = Path(value).resolve(strict=False)
        if not any(
            target == root or root in target.parents
            for root in self._settings.output_roots
        ):
            raise GlobalDeduplicationProcessingError(
                GlobalDeduplicationErrorCode.OUTPUT_PATH_NOT_ALLOWED,
                "目标结果路径不在允许范围内",
            )
        return target

    def _fail(
        self,
        task: GlobalDeduplicationTask,
        error: GlobalDeduplicationProcessingError,
    ) -> None:
        try:
            self._repository.transition(
                task.id,
                expected=GlobalDeduplicationTaskStatus.RUNNING,
                target=GlobalDeduplicationTaskStatus.FAILED,
                error_code=error.code,
                error_message=error.safe_message,
                finished_at=self._now(),
                lease_expires_at=None,
                poll_lease_expires_at=None,
                next_poll_at=None,
                processing_phase=None,
                updated_at=self._now(),
            )
        except ConditionalGlobalDeduplicationUpdateFailed:
            return

    @staticmethod
    def _deadline_expired(
        task: GlobalDeduplicationTask,
        now: datetime,
    ) -> bool:
        if task.processing_deadline is None:
            return False
        deadline = task.processing_deadline
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        return deadline <= now

    @staticmethod
    def _job_not_found_was_resubmitted(
        task: GlobalDeduplicationTask,
    ) -> bool:
        return (
            task.external_progress is not None
            and task.external_progress.get("jobNotFoundResubmitted") is True
        )

    @staticmethod
    def _internal_error() -> GlobalDeduplicationProcessingError:
        return GlobalDeduplicationProcessingError(
            GlobalDeduplicationErrorCode.INTERNAL_ERROR,
            "全局去重处理失败",
        )
