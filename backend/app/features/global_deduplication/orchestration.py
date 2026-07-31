import hashlib
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from app.core.config import GlobalDeduplicationWorkerSettings
from app.features.global_deduplication.adapters.datajuicer import (
    DataJuicerAdapter,
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
from app.features.global_deduplication.repository import (
    ConditionalGlobalDeduplicationUpdateFailed,
    GlobalDeduplicationTaskRepository,
)
from app.features.global_deduplication.staging import GlobalDeduplicationStaging
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
    def enqueue_poll(self, task_id: uuid.UUID, *, countdown: int) -> None: ...


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
    ) -> None:
        self._repository = repository
        self._reader = reader
        self._staging = staging
        self._adapter = adapter
        self._scheduler = scheduler
        self._settings = settings
        self._now = now

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
