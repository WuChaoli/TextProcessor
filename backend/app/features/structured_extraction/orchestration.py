import hashlib
import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

import httpx
from sqlmodel import Session, col, select

from app.core.config import ExtractionWorkerSettings, settings
from app.features.structured_extraction.adapters.docling import DoclingHttpAdapter
from app.features.structured_extraction.adapters.mineru import MinerUHttpAdapter
from app.features.structured_extraction.adapters.protocol import (
    ExternalProcessorAdapter,
)
from app.features.structured_extraction.errors import (
    ExtractionErrorCode,
    ExtractionProcessingError,
)
from app.features.structured_extraction.format_detector import FormatDetector
from app.features.structured_extraction.input_resolver import InputResolver
from app.features.structured_extraction.models import (
    ExtractionTask,
    ExtractionTaskStatus,
    ProcessorSlot,
    get_datetime_utc,
)
from app.features.structured_extraction.office_inspector import OfficeDocumentInspector
from app.features.structured_extraction.processors.markdown_normalizer import (
    MarkdownNormalizer,
)
from app.features.structured_extraction.processors.plain_text import (
    PlainTextPassThroughProcessor,
)
from app.features.structured_extraction.processors.publisher import AtomicPublisher
from app.features.structured_extraction.repository import (
    ConditionalTransitionFailed,
    ExtractionTaskRepository,
)
from app.features.structured_extraction.request_policy import RequestPolicy
from app.features.structured_extraction.router import ProcessorRouter
from app.features.structured_extraction.slots import ProcessorSlotRepository
from app.features.structured_extraction.staging import StagingLayout
from app.features.structured_extraction.worker_models import (
    DetectedFormat,
    ExternalTaskState,
    ProcessingContext,
    ProcessorArtifact,
    ProcessorName,
)

_TERMINAL_STATUSES = frozenset(
    {
        ExtractionTaskStatus.SUCCEEDED,
        ExtractionTaskStatus.FAILED,
        ExtractionTaskStatus.CANCELLED,
    }
)
logger = logging.getLogger(__name__)


def is_retryable_processor_http_error(error: ExtractionProcessingError) -> bool:
    return error.transient and error.code is ExtractionErrorCode.PROCESSING_FAILED


class ExtractionTaskScheduler(Protocol):
    def enqueue_submit(self, task_id: uuid.UUID, *, countdown: int) -> None: ...

    def enqueue_poll(self, task_id: uuid.UUID, *, countdown: int) -> None: ...


def _s3_storage_options(
    worker_settings: ExtractionWorkerSettings,
) -> dict[str, object]:
    options: dict[str, object] = {}
    client_kwargs: dict[str, str] = {}
    if worker_settings.s3_endpoint_url is not None:
        client_kwargs["endpoint_url"] = str(worker_settings.s3_endpoint_url).rstrip("/")
    if worker_settings.s3_region is not None:
        client_kwargs["region_name"] = worker_settings.s3_region
    if client_kwargs:
        options["client_kwargs"] = client_kwargs
    if worker_settings.s3_access_key_id is not None:
        options["key"] = worker_settings.s3_access_key_id
    if worker_settings.s3_secret_access_key is not None:
        options["secret"] = worker_settings.s3_secret_access_key
    return options


class _NoopScheduler:
    def enqueue_submit(self, task_id: uuid.UUID, *, countdown: int) -> None:
        del task_id, countdown

    def enqueue_poll(self, task_id: uuid.UUID, *, countdown: int) -> None:
        del task_id, countdown


class ExtractionOrchestrator:
    def __init__(
        self,
        session: Session,
        *,
        worker_settings: ExtractionWorkerSettings,
        input_roots: tuple[Path, ...],
        max_input_bytes: int,
        scheduler: ExtractionTaskScheduler | None = None,
        adapter_factory: Callable[[ProcessorName], ExternalProcessorAdapter]
        | None = None,
        slots: ProcessorSlotRepository | None = None,
        remote_url_validator: Callable[[str], str] | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._session = session
        self._settings = worker_settings
        self._repository = ExtractionTaskRepository(session)
        self._scheduler = scheduler or _NoopScheduler()
        self._adapter_factory = adapter_factory
        self._slots = slots or ProcessorSlotRepository(session)
        validator = (
            remote_url_validator
            or RequestPolicy(
                input_roots=input_roots,
                output_roots=worker_settings.output_roots,
                allowed_http_hosts=settings.EXTRACTION_HTTP_ALLOWED_HOSTS,
                allowed_http_cidrs=settings.EXTRACTION_HTTP_ALLOWED_CIDRS,
                max_input_bytes=max_input_bytes,
            ).validate_remote_url
        )
        self._resolver = InputResolver(
            input_roots=input_roots,
            max_input_bytes=max_input_bytes,
            copy_chunk_bytes=worker_settings.copy_chunk_bytes,
            remote_url_validator=validator,
            allowed_s3_buckets=worker_settings.s3_allowed_buckets,
            s3_storage_options=_s3_storage_options(worker_settings),
            http_client=http_client,
            max_http_redirects=worker_settings.max_http_redirects,
        )
        self._detector = FormatDetector()
        self._inspector = OfficeDocumentInspector()
        self._router = ProcessorRouter(
            production_formats=worker_settings.production_formats,
            docx_visual_complexity_threshold=(
                worker_settings.docx_visual_complexity_threshold
            ),
        )
        self._text_processor = PlainTextPassThroughProcessor()
        self._normalizer = MarkdownNormalizer()
        self._publisher = AtomicPublisher(
            max_output_bytes=worker_settings.max_output_bytes,
            output_roots=worker_settings.output_roots,
            copy_chunk_bytes=worker_settings.copy_chunk_bytes,
        )

    def submit(self, task_id: uuid.UUID) -> None:
        task = self._session.get(ExtractionTask, task_id)
        if task is None:
            return
        if (
            task.status is ExtractionTaskStatus.RUNNING
            and task.external_task_id is None
            and task.processing_phase == "submitting"
        ):
            self._resume_external_submission(task)
            return
        if task.status is not ExtractionTaskStatus.QUEUED:
            return
        try:
            self._publisher.ensure_target_available(Path(task.target_path))
            prepared = self._prepare_queued_task(task)
        except ExtractionProcessingError as error:
            self._fail_queued(task, error)
            return
        except Exception:
            self._session.rollback()
            self._fail_queued(task, self._internal_error())
            return

        if prepared.processor is ProcessorName.PLAIN_TEXT:
            self._submit_plain_text(task, prepared.layout)
            return
        self._submit_external(task, prepared.layout, prepared.processor)

    def poll(self, task_id: uuid.UUID, *, now: datetime | None = None) -> None:
        current_time = now or get_datetime_utc()
        task = self._repository.claim_poll(
            task_id,
            now=current_time,
            lease_duration=timedelta(seconds=self._settings.poll_lease_seconds),
        )
        if task is None:
            return
        if self._deadline_expired(task, current_time):
            self._fail_running(
                task,
                ExtractionProcessingError(
                    ExtractionErrorCode.PROCESSING_TIMEOUT,
                    "结构化提取处理超时",
                ),
                quarantine_slot=True,
            )
            return

        try:
            processor = self._external_processor(task)
        except ExtractionProcessingError as error:
            self._fail_running(task, error, release_slot=True)
            return
        external_task_id = task.external_task_id
        if external_task_id is None:
            self._fail_running(task, self._internal_error())
            return
        try:
            status = self._adapter_for(processor).get_status(external_task_id)
        except ExtractionProcessingError as error:
            if is_retryable_processor_http_error(error):
                self._schedule_poll_retry(task, current_time)
                raise
            self._fail_running(task, error)
            return
        except Exception:
            self._fail_running(task, self._internal_error())
            return

        if status.state is ExternalTaskState.PROCESSING:
            self._schedule_next_poll(task, current_time)
            return
        if status.state is ExternalTaskState.FAILED:
            self._fail_running(
                task,
                ExtractionProcessingError(
                    self._error_code_or_default(status.safe_error_code),
                    status.safe_error_message or "外部处理器处理失败",
                ),
                release_slot=True,
            )
            return

        self._slots.release(task.id)
        self._finalize_success(task, processor, external_task_id, current_time)

    def recover(self, *, now: datetime | None = None) -> int:
        current_time = now or get_datetime_utc()
        recovered = 0
        for task in self._repository.list_expired_running(
            now=current_time,
            limit=self._settings.recovery_batch_size,
        ):
            self._fail_running(
                task,
                ExtractionProcessingError(
                    ExtractionErrorCode.PROCESSING_TIMEOUT,
                    "结构化提取处理超时",
                ),
                quarantine_slot=True,
            )
            recovered += 1
        for task in self._repository.list_recoverable_queued(
            now=current_time,
            limit=self._settings.recovery_batch_size,
        ):
            try:
                self._scheduler.enqueue_submit(task.id, countdown=0)
                if self._repository.mark_recovery_submit_scheduled(
                    task.id,
                    next_retry_at=current_time
                    + timedelta(seconds=self._settings.poll_interval_seconds),
                ):
                    recovered += 1
            except Exception:
                self._repository.rollback()
        for task in self._repository.list_recoverable_polls(
            now=current_time,
            limit=self._settings.recovery_batch_size,
        ):
            try:
                self._scheduler.enqueue_poll(task.id, countdown=0)
                recovered += 1
            except Exception:
                self._repository.rollback()
        recovered += self._reconcile_orphaned_slots()
        recovered += self._reap_quarantined_slots(current_time)
        for task in self._repository.list_terminal_with_staging(
            limit=self._settings.recovery_batch_size
        ):
            if self._cleanup_terminal_staging(task):
                recovered += 1
        return recovered

    def fail_exhausted_submission_retry(
        self,
        task_id: uuid.UUID,
        error: ExtractionProcessingError,
    ) -> None:
        """Close an external submission when Celery cannot safely retry again."""
        if not is_retryable_processor_http_error(error):
            return
        task = self._session.get(ExtractionTask, task_id)
        if (
            task is None
            or task.status is not ExtractionTaskStatus.RUNNING
            or task.external_task_id is not None
            or task.processing_phase != "submitting"
        ):
            return
        self._fail_submission(
            task,
            ExtractionProcessingError(
                ExtractionErrorCode.PROCESSOR_SUBMISSION_UNCERTAIN,
                "外部处理器提交重试耗尽，无法确认提交结果",
            ),
        )

    def _prepare_queued_task(self, task: ExtractionTask) -> _PreparedTask:
        layout = StagingLayout.for_task(self._settings.staging_root, task.id)
        resolved = self._resolver.resolve(task, layout)
        processor = self._persist_or_reuse_routing(
            task,
            resolved.path,
            resolved.sha256,
            resolved.size_bytes,
        )
        return _PreparedTask(layout=layout, processor=processor)

    def _persist_or_reuse_routing(
        self,
        task: ExtractionTask,
        source: Path,
        input_sha256: str,
        input_size_bytes: int,
    ) -> ProcessorName:
        if task.processor_name is None:
            document = self._detector.detect(source)
            inspection = (
                self._inspector.inspect_docx(source)
                if document.detected_format is DetectedFormat.DOCX
                else None
            )
            decision = self._router.route(document, inspection)
            processor = decision.processor
            self._repository.update_queued(
                task.id,
                input_sha256=input_sha256,
                input_size_bytes=input_size_bytes,
                detected_format=decision.detected_format,
                processor_name=processor.value,
                routing_reasons=list(decision.reasons),
                processing_phase="staging",
                staging_path=str(
                    StagingLayout.for_task(self._settings.staging_root, task.id).root
                ),
            )
            return processor
        try:
            processor = ProcessorName(task.processor_name)
        except ValueError:
            raise self._internal_error() from None
        self._repository.update_queued(
            task.id,
            input_sha256=input_sha256,
            input_size_bytes=input_size_bytes,
            staging_path=str(
                StagingLayout.for_task(self._settings.staging_root, task.id).root
            ),
        )
        return processor

    def _submit_plain_text(self, task: ExtractionTask, layout: StagingLayout) -> None:
        now = get_datetime_utc()
        try:
            running = self._repository.transition(
                task.id,
                expected=ExtractionTaskStatus.QUEUED,
                target=ExtractionTaskStatus.RUNNING,
                started_at=now,
                attempt_count=task.attempt_count + 1,
                lease_expires_at=now
                + timedelta(seconds=self._settings.processing_deadline_seconds),
                processing_phase="processing",
            )
        except ConditionalTransitionFailed:
            return
        try:
            source = self._resolver.resolve(running, layout).path
            artifact = self._text_processor.process(source, layout.output)
            self._publish_artifact(running, artifact, layout, allow_recovery=False)
        except ExtractionProcessingError as error:
            self._fail_running(running, error)
        except Exception:
            self._session.rollback()
            self._fail_running(running, self._internal_error())

    def _submit_external(
        self,
        task: ExtractionTask,
        layout: StagingLayout,
        processor: ProcessorName,
    ) -> None:
        now = get_datetime_utc()
        slot = self._slots.acquire(
            task_id=task.id,
            processor_name=processor.value,
            max_in_flight=self._max_in_flight(processor),
            lease_duration=timedelta(
                seconds=self._settings.processing_deadline_seconds
            ),
            now=now,
        )
        if slot is None:
            self._repository.update_queued(
                task.id,
                processing_phase="waiting_capacity",
                next_poll_at=now
                + timedelta(seconds=self._settings.poll_interval_seconds),
            )
            self._scheduler.enqueue_submit(
                task.id,
                countdown=self._settings.poll_interval_seconds,
            )
            return
        try:
            running = self._repository.transition(
                task.id,
                expected=ExtractionTaskStatus.QUEUED,
                target=ExtractionTaskStatus.RUNNING,
                started_at=now,
                attempt_count=task.attempt_count + 1,
                lease_expires_at=now
                + timedelta(seconds=self._settings.processing_deadline_seconds),
                processing_deadline=now
                + timedelta(seconds=self._settings.processing_deadline_seconds),
                processing_phase="submitting",
                next_poll_at=None,
            )
        except ConditionalTransitionFailed:
            return
        self._submit_to_external(running, layout, processor)

    def _resume_external_submission(self, task: ExtractionTask) -> None:
        try:
            processor = self._external_processor(task)
            layout = StagingLayout.for_task(self._settings.staging_root, task.id)
        except ExtractionProcessingError as error:
            self._fail_running(task, error, release_slot=True)
            return
        self._submit_to_external(task, layout, processor)

    def _submit_to_external(
        self,
        task: ExtractionTask,
        layout: StagingLayout,
        processor: ProcessorName,
    ) -> None:
        try:
            source = self._resolver.resolve(task, layout).path
            submission = self._adapter_for(processor).submit(
                source,
                self._processing_context(task, processor),
            )
            if submission.processor_name is not processor:
                raise ExtractionProcessingError(
                    ExtractionErrorCode.INVALID_PROCESSOR_OUTPUT,
                    "外部处理器返回了错误的处理器标识",
                )
        except ExtractionProcessingError as error:
            if is_retryable_processor_http_error(error):
                raise
            self._fail_submission(task, error)
            return
        except Exception:
            self._fail_submission(task, self._internal_error())
            return

        submitted_at = get_datetime_utc()
        if not self._repository.update_running(
            task.id,
            external_task_id=submission.external_task_id,
            processor_version=submission.processor_version,
            profile_name=self._profile_name(processor),
            profile_sha256=self._profile_sha256(processor),
            next_poll_at=submitted_at
            + timedelta(seconds=self._settings.poll_interval_seconds),
            poll_lease_expires_at=None,
            processing_phase="submitted",
        ):
            return
        self._scheduler.enqueue_poll(
            task.id,
            countdown=self._settings.poll_interval_seconds,
        )

    def _finalize_success(
        self,
        task: ExtractionTask,
        processor: ProcessorName,
        external_task_id: str,
        now: datetime,
    ) -> None:
        layout = StagingLayout.for_task(self._settings.staging_root, task.id)
        try:
            self._repository.update_running(task.id, processing_phase="downloading")
            artifact = self._adapter_for(processor).fetch_result(
                external_task_id,
                layout.processor_dir / "result.md",
            )
            self._repository.update_running(task.id, processing_phase="normalizing")
            normalized = self._normalizer.normalize(
                artifact.markdown_path.read_text(encoding="utf-8")
            )
            layout.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            layout.output.write_text(normalized, encoding="utf-8", newline="")
            normalized_artifact = ProcessorArtifact(
                markdown_path=layout.output,
                processor_name=artifact.processor_name,
                processor_version=artifact.processor_version,
                profile_name=artifact.profile_name,
                profile_sha256=artifact.profile_sha256,
            )
            self._publish_artifact(
                task, normalized_artifact, layout, allow_recovery=True
            )
        except ExtractionProcessingError as error:
            if is_retryable_processor_http_error(error):
                self._schedule_poll_retry(task, now)
                raise
            self._fail_running(task, error)
        except OSError:
            output_error = ExtractionProcessingError(
                ExtractionErrorCode.OUTPUT_WRITE_FAILED,
                "无法写入处理器归一化结果",
            )
            self._fail_running(task, output_error)
        except Exception:
            self._session.rollback()
            self._fail_running(task, self._internal_error())

    def _publish_artifact(
        self,
        task: ExtractionTask,
        artifact: ProcessorArtifact,
        layout: StagingLayout,
        *,
        allow_recovery: bool,
    ) -> None:
        self._repository.update_running(task.id, processing_phase="publishing")
        prepared = self._publisher.prepare(artifact.markdown_path)
        layout.manifest.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "taskId": str(task.id),
                    "detectedFormat": task.detected_format,
                    "inputSha256": task.input_sha256,
                    "output": {
                        "path": task.target_path,
                        "sha256": prepared.sha256,
                        "sizeBytes": prepared.size_bytes,
                    },
                    "processor": {
                        "name": artifact.processor_name.value,
                        "version": artifact.processor_version,
                        "profile": artifact.profile_name,
                        "profileSha256": artifact.profile_sha256,
                    },
                    "routing": {"reasons": task.routing_reasons or []},
                    "publication": {"atomicFilePublish": True},
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
            newline="",
        )
        published = self._publisher.publish(
            prepared,
            Path(task.target_path),
            allow_recovery=allow_recovery,
        )
        completed = self._repository.transition(
            task.id,
            expected=ExtractionTaskStatus.RUNNING,
            target=ExtractionTaskStatus.SUCCEEDED,
            processor_version=artifact.processor_version,
            profile_name=artifact.profile_name,
            profile_sha256=artifact.profile_sha256,
            output_sha256=published.sha256,
            prepared_output_sha256=published.sha256,
            published_at=get_datetime_utc(),
            finished_at=get_datetime_utc(),
            lease_expires_at=None,
            poll_lease_expires_at=None,
            next_poll_at=None,
            processing_phase=None,
            result_metadata={
                "target_path": task.target_path,
                "output_size_bytes": published.size_bytes,
            },
        )
        self._cleanup_terminal_staging(completed)

    def _schedule_next_poll(self, task: ExtractionTask, now: datetime) -> None:
        if not self._repository.update_running(
            task.id,
            processing_phase="submitted",
            poll_lease_expires_at=None,
            next_poll_at=now + timedelta(seconds=self._settings.poll_interval_seconds),
        ):
            return
        self._slots.refresh(
            task.id,
            lease_duration=timedelta(
                seconds=self._settings.processing_deadline_seconds
            ),
            now=now,
        )
        self._scheduler.enqueue_poll(
            task.id,
            countdown=self._settings.poll_interval_seconds,
        )

    def _schedule_poll_retry(self, task: ExtractionTask, now: datetime) -> None:
        self._repository.update_running(
            task.id,
            processing_phase="submitted",
            poll_lease_expires_at=None,
            next_poll_at=now,
        )

    def _fail_submission(
        self,
        task: ExtractionTask,
        error: ExtractionProcessingError,
    ) -> None:
        self._fail_running(
            task,
            error,
            quarantine_slot=(
                error.code is ExtractionErrorCode.PROCESSOR_SUBMISSION_UNCERTAIN
                or error.transient
            ),
            release_slot=(
                error.code is not ExtractionErrorCode.PROCESSOR_SUBMISSION_UNCERTAIN
                and not error.transient
            ),
        )

    def _fail_queued(
        self,
        task: ExtractionTask,
        error: ExtractionProcessingError,
    ) -> None:
        try:
            failed = self._repository.transition(
                task.id,
                expected=ExtractionTaskStatus.QUEUED,
                target=ExtractionTaskStatus.FAILED,
                error_code=error.code,
                error_message=error.safe_message,
                finished_at=get_datetime_utc(),
                processing_phase=None,
                next_poll_at=None,
            )
        except ConditionalTransitionFailed:
            return
        self._cleanup_terminal_staging(failed)

    def _fail_running(
        self,
        task: ExtractionTask,
        error: ExtractionProcessingError,
        *,
        release_slot: bool = False,
        quarantine_slot: bool = False,
    ) -> None:
        try:
            failed = self._repository.transition(
                task.id,
                expected=ExtractionTaskStatus.RUNNING,
                target=ExtractionTaskStatus.FAILED,
                error_code=error.code,
                error_message=error.safe_message,
                finished_at=get_datetime_utc(),
                lease_expires_at=None,
                poll_lease_expires_at=None,
                next_poll_at=None,
                processing_phase=None,
            )
        except ConditionalTransitionFailed:
            return
        self._cleanup_terminal_staging(failed)
        if quarantine_slot:
            self._slots.quarantine(task.id)
        elif release_slot:
            self._slots.release(task.id)

    def _cleanup_terminal_staging(self, task: ExtractionTask) -> bool:
        if task.status not in _TERMINAL_STATUSES or task.staging_path is None:
            return False
        layout = StagingLayout.for_task(self._settings.staging_root, task.id)
        if Path(task.staging_path).resolve(strict=False) != layout.root:
            logger.warning("拒绝清理不匹配的任务 staging", extra={"task_id": str(task.id)})
            return False
        try:
            layout.cleanup()
        except (OSError, ValueError):
            logger.warning(
                "清理结构化提取任务 staging 失败",
                extra={"task_id": str(task.id)},
                exc_info=True,
            )
            return False
        return self._repository.clear_terminal_staging(task.id)

    def _reconcile_orphaned_slots(self) -> int:
        slots = list(
            self._session.exec(
                select(ProcessorSlot)
                .order_by(col(ProcessorSlot.acquired_at), col(ProcessorSlot.id))
                .limit(self._settings.recovery_batch_size)
            ).all()
        )
        reconciled = 0
        for slot in slots:
            task = self._session.get(ExtractionTask, slot.task_id)
            if task is None:
                self._session.delete(slot)
                self._session.commit()
                reconciled += 1
            elif task.status in _TERMINAL_STATUSES and slot.state == "active":
                if self._slots.release(task.id):
                    reconciled += 1
        return reconciled

    def _reap_quarantined_slots(self, now: datetime) -> int:
        cutoff = now - timedelta(seconds=self._settings.slot_quarantine_grace_seconds)
        expired_slots = list(
            self._session.exec(
                select(ProcessorSlot)
                .where(
                    ProcessorSlot.state == "quarantined",
                    col(ProcessorSlot.quarantined_at).is_not(None),
                    col(ProcessorSlot.quarantined_at) <= cutoff,
                )
                .order_by(col(ProcessorSlot.quarantined_at), col(ProcessorSlot.id))
                .limit(self._settings.recovery_batch_size)
            ).all()
        )
        for slot in expired_slots:
            self._session.delete(slot)
        if expired_slots:
            self._session.commit()
        return len(expired_slots)

    def _adapter_for(self, processor: ProcessorName) -> ExternalProcessorAdapter:
        if self._adapter_factory is not None:
            return self._adapter_factory(processor)
        timeout = httpx.Timeout(
            connect=self._settings.connect_timeout_seconds,
            read=self._settings.read_timeout_seconds,
            write=self._settings.read_timeout_seconds,
            pool=self._settings.connect_timeout_seconds,
        )
        client = httpx.Client(timeout=timeout)
        if processor is ProcessorName.MINERU:
            if self._settings.mineru_base_url is None:
                raise ExtractionProcessingError(
                    ExtractionErrorCode.PROCESSING_FAILED,
                    "MinerU 处理器未配置",
                )
            return MinerUHttpAdapter(
                base_url=str(self._settings.mineru_base_url),
                profile=self._settings.mineru_profile,
                profile_name=self._settings.mineru_profile_name,
                api_key=self._settings.mineru_api_key,
                client=client,
                max_result_bytes=self._settings.max_output_bytes,
            )
        if processor is ProcessorName.DOCLING:
            if self._settings.docling_base_url is None:
                raise ExtractionProcessingError(
                    ExtractionErrorCode.PROCESSING_FAILED,
                    "Docling 处理器未配置",
                )
            return DoclingHttpAdapter(
                base_url=str(self._settings.docling_base_url),
                profile=self._settings.docling_profile,
                profile_name=self._settings.docling_profile_name,
                api_key=self._settings.docling_api_key,
                client=client,
                max_result_bytes=self._settings.max_output_bytes,
            )
        raise self._internal_error()

    def _processing_context(
        self,
        task: ExtractionTask,
        processor: ProcessorName,
    ) -> ProcessingContext:
        detected_format = task.detected_format
        if detected_format is None:
            raise self._internal_error()
        try:
            return ProcessingContext(
                task_id=task.id,
                detected_format=DetectedFormat(detected_format),
                profile_name=self._profile_name(processor),
                profile_sha256=self._profile_sha256(processor),
            )
        except ValueError:
            raise self._internal_error() from None

    def _profile_name(self, processor: ProcessorName) -> str:
        if processor is ProcessorName.MINERU:
            return self._settings.mineru_profile_name
        if processor is ProcessorName.DOCLING:
            return self._settings.docling_profile_name
        raise self._internal_error()

    def _profile_sha256(self, processor: ProcessorName) -> str:
        profile = (
            self._settings.mineru_profile
            if processor is ProcessorName.MINERU
            else self._settings.docling_profile
        )
        serialized = json.dumps(
            profile.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(serialized.encode()).hexdigest()

    def _max_in_flight(self, processor: ProcessorName) -> int:
        if processor is ProcessorName.MINERU:
            return self._settings.mineru_max_in_flight_tasks
        if processor is ProcessorName.DOCLING:
            return self._settings.docling_max_in_flight_tasks
        raise self._internal_error()

    @staticmethod
    def _error_code_or_default(value: str | None) -> ExtractionErrorCode:
        try:
            return ExtractionErrorCode(value or ExtractionErrorCode.PROCESSING_FAILED)
        except ValueError:
            return ExtractionErrorCode.PROCESSING_FAILED

    @staticmethod
    def _deadline_expired(task: ExtractionTask, now: datetime) -> bool:
        if task.processing_deadline is None:
            return False
        deadline = task.processing_deadline
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        return deadline <= now

    @staticmethod
    def _external_processor(task: ExtractionTask) -> ProcessorName:
        try:
            processor = ProcessorName(task.processor_name or "")
        except ValueError:
            raise ExtractionProcessingError(
                ExtractionErrorCode.INTERNAL_ERROR,
                "外部处理器任务缺少有效处理器标识",
            ) from None
        if processor is ProcessorName.PLAIN_TEXT:
            raise ExtractionProcessingError(
                ExtractionErrorCode.INTERNAL_ERROR,
                "文本任务不应进入外部轮询",
            )
        return processor

    @staticmethod
    def _internal_error() -> ExtractionProcessingError:
        return ExtractionProcessingError(
            ExtractionErrorCode.INTERNAL_ERROR,
            "结构化提取处理失败",
        )


@dataclass(frozen=True)
class _PreparedTask:
    layout: StagingLayout
    processor: ProcessorName
