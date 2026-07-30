import uuid
from pathlib import Path

from sqlmodel import Session

from app.core.config import ExtractionWorkerSettings
from app.features.structured_extraction.errors import (
    ExtractionErrorCode,
    ExtractionProcessingError,
)
from app.features.structured_extraction.format_detector import FormatDetector
from app.features.structured_extraction.input_resolver import InputResolver
from app.features.structured_extraction.models import (
    ExtractionTask,
    ExtractionTaskStatus,
    get_datetime_utc,
)
from app.features.structured_extraction.office_inspector import OfficeDocumentInspector
from app.features.structured_extraction.processors.plain_text import (
    PlainTextPassThroughProcessor,
)
from app.features.structured_extraction.processors.publisher import AtomicPublisher
from app.features.structured_extraction.repository import (
    ConditionalTransitionFailed,
    ExtractionTaskRepository,
)
from app.features.structured_extraction.router import ProcessorRouter, RoutingDecision
from app.features.structured_extraction.staging import StagingLayout
from app.features.structured_extraction.worker_models import (
    DetectedFormat,
    ProcessorName,
)


class ExtractionOrchestrator:
    def __init__(
        self,
        session: Session,
        *,
        worker_settings: ExtractionWorkerSettings,
        input_roots: tuple[Path, ...],
        max_input_bytes: int,
    ) -> None:
        self._session = session
        self._settings = worker_settings
        self._repository = ExtractionTaskRepository(session)
        self._resolver = InputResolver(
            input_roots=input_roots,
            max_input_bytes=max_input_bytes,
            copy_chunk_bytes=worker_settings.copy_chunk_bytes,
            allowed_s3_buckets=worker_settings.s3_allowed_buckets,
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
        self._publisher = AtomicPublisher(
            max_output_bytes=worker_settings.max_output_bytes,
            output_roots=worker_settings.output_roots,
            copy_chunk_bytes=worker_settings.copy_chunk_bytes,
        )

    def submit(self, task_id: uuid.UUID) -> None:
        task = self._session.get(ExtractionTask, task_id)
        if task is None or task.status is not ExtractionTaskStatus.QUEUED:
            return
        target = Path(task.target_path)
        try:
            self._publisher.ensure_target_available(target)
        except ExtractionProcessingError as error:
            self._fail_queued(task, error)
            return

        try:
            running = self._repository.transition(
                task.id,
                expected=ExtractionTaskStatus.QUEUED,
                target=ExtractionTaskStatus.RUNNING,
                started_at=get_datetime_utc(),
                attempt_count=task.attempt_count + 1,
                processing_phase="staging",
            )
        except ConditionalTransitionFailed:
            return

        layout = StagingLayout.for_task(self._settings.staging_root, running.id)
        try:
            resolved = self._resolver.resolve(running, layout)
            document = self._detector.detect(resolved.path)
            inspection = (
                self._inspector.inspect_docx(resolved.path)
                if document.detected_format is DetectedFormat.DOCX
                else None
            )
            decision = self._router.route(document, inspection)
            self._persist_routing(
                running, resolved.sha256, resolved.size_bytes, decision
            )
            if decision.processor is not ProcessorName.PLAIN_TEXT:
                raise ExtractionProcessingError(
                    ExtractionErrorCode.PROCESSING_FAILED,
                    "外部文档处理器尚未接入 worker 编排",
                )
            artifact = self._text_processor.process(resolved.path, layout.output)
            prepared = self._publisher.prepare(artifact.markdown_path)
            self._set_phase(running, "publishing")
            published = self._publisher.publish(prepared, target)
            self._repository.transition(
                running.id,
                expected=ExtractionTaskStatus.RUNNING,
                target=ExtractionTaskStatus.SUCCEEDED,
                processor_version=artifact.processor_version,
                profile_name=artifact.profile_name,
                profile_sha256=artifact.profile_sha256,
                output_sha256=published.sha256,
                prepared_output_sha256=published.sha256,
                published_at=get_datetime_utc(),
                finished_at=get_datetime_utc(),
                processing_phase=None,
                result_metadata={
                    "target_path": task.target_path,
                    "output_size_bytes": published.size_bytes,
                },
            )
            layout.cleanup()
        except ExtractionProcessingError as error:
            self._fail_running(running, error)
        except Exception:
            self._session.rollback()
            self._fail_running(
                running,
                ExtractionProcessingError(
                    ExtractionErrorCode.INTERNAL_ERROR,
                    "结构化提取处理失败",
                ),
            )

    def _persist_routing(
        self,
        task: ExtractionTask,
        input_sha256: str,
        input_size_bytes: int,
        decision: RoutingDecision,
    ) -> None:
        task.input_sha256 = input_sha256
        task.input_size_bytes = input_size_bytes
        task.detected_format = decision.detected_format
        task.processor_name = decision.processor.value
        task.routing_reasons = list(decision.reasons)
        task.processing_phase = "processing"
        task.staging_path = str(
            StagingLayout.for_task(self._settings.staging_root, task.id).root
        )
        task.updated_at = get_datetime_utc()
        self._session.add(task)
        self._session.commit()

    def _set_phase(self, task: ExtractionTask, phase: str) -> None:
        task.processing_phase = phase
        task.updated_at = get_datetime_utc()
        self._session.add(task)
        self._session.commit()

    def _fail_queued(
        self,
        task: ExtractionTask,
        error: ExtractionProcessingError,
    ) -> None:
        try:
            self._repository.transition(
                task.id,
                expected=ExtractionTaskStatus.QUEUED,
                target=ExtractionTaskStatus.FAILED,
                error_code=error.code,
                error_message=error.safe_message,
                finished_at=get_datetime_utc(),
            )
        except ConditionalTransitionFailed:
            return

    def _fail_running(
        self,
        task: ExtractionTask,
        error: ExtractionProcessingError,
    ) -> None:
        try:
            self._repository.transition(
                task.id,
                expected=ExtractionTaskStatus.RUNNING,
                target=ExtractionTaskStatus.FAILED,
                error_code=error.code,
                error_message=error.safe_message,
                finished_at=get_datetime_utc(),
                processing_phase=None,
            )
        except ConditionalTransitionFailed:
            return
