from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread
from typing import Protocol

from app.features.markdown_cleaning.input_validator import (
    MarkdownInputError,
    ValidatedMarkdownInput,
)
from app.features.markdown_cleaning.processors.errors import (
    MarkdownCleaningErrorCode,
    MarkdownCleaningProcessorError,
)
from app.features.markdown_cleaning.processors.models import ProcessorResult
from app.features.markdown_cleaning.processors.protocol import MarkdownCleaningProcessor
from app.features.markdown_cleaning.publisher import (
    InvalidPreparedOutputError,
    OutputConflictError,
    PreparedMarkdownResult,
    PublicationSystemError,
    PublishedMarkdownResult,
)
from app.features.markdown_cleaning.staging import StagingLayout
from app.features.markdown_cleaning.worker_models import MarkdownCleaningWorkerTask


class LeaseLostError(RuntimeError):
    """The worker no longer owns the task and must stop without writing state."""


class RetryableWorkerError(RuntimeError):
    """A transient failure that the bounded task runner may retry."""


class LeaseHeartbeat:
    """Renew a lease through a callback backed by an independent DB session."""

    def __init__(self, renew: Callable[[], bool], interval_seconds: float) -> None:
        if interval_seconds <= 0:
            raise ValueError("heartbeat interval 必须大于 0")
        self._renew = renew
        self._interval = interval_seconds
        self._stop = Event()
        self._lost = Event()
        self._thread: Thread | None = None

    def __enter__(self) -> LeaseHeartbeat:
        if not self._renew():
            raise LeaseLostError("租约续期失败")
        self._thread = Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        if self._lost.is_set():
            raise LeaseLostError("处理期间租约已失效")

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                renewed = self._renew()
            except Exception:
                renewed = False
            if not renewed:
                self._lost.set()
                self._stop.set()


class _Repository(Protocol):
    def acquire_queued(
        self,
        task_id: uuid.UUID,
        *,
        now: datetime,
        lease_seconds: int,
        processing_timeout_seconds: int,
    ) -> MarkdownCleaningWorkerTask | None: ...

    def update_progress(self, task_id: uuid.UUID, **kwargs: object) -> bool: ...

    def save_prepared(self, task_id: uuid.UUID, **kwargs: object) -> bool: ...

    def mark_publishing(self, task_id: uuid.UUID, **kwargs: object) -> bool: ...

    def mark_succeeded(self, task_id: uuid.UUID, **kwargs: object) -> bool: ...

    def mark_failed(self, task_id: uuid.UUID, **kwargs: object) -> bool: ...


class _Resolver(Protocol):
    def resolve(
        self, task: MarkdownCleaningWorkerTask, layout: StagingLayout
    ) -> object: ...


class _InputValidator(Protocol):
    def validate(
        self, resolved: object, layout: StagingLayout, **kwargs: object
    ) -> ValidatedMarkdownInput: ...


class _OutputValidator(Protocol):
    def validate(
        self, result: ProcessorResult, **kwargs: object
    ) -> ProcessorResult: ...


class _Publisher(Protocol):
    def prepare(self, source: Path) -> PreparedMarkdownResult: ...

    def publish(
        self, prepared: PreparedMarkdownResult, target: Path, *, allow_recovery: bool
    ) -> PublishedMarkdownResult: ...


class _Dispatcher(Protocol):
    def enqueue_execute(self, task_id: uuid.UUID) -> None: ...


class _RecoveryTask(Protocol):
    id: uuid.UUID
    lease_token: str | None
    target_path: str
    prepared_output_sha256: str
    staging_path: str


class _RecoveryRepository(Protocol):
    def list_recoverable_queued(self, **kwargs: object) -> list[_RecoveryTask]: ...

    def mark_recovery_dispatched(
        self, task_id: uuid.UUID, **kwargs: object
    ) -> bool: ...

    def list_recoverable_running(self, **kwargs: object) -> list[_RecoveryTask]: ...

    def recover_expired_running(self, task_id: uuid.UUID, **kwargs: object) -> bool: ...

    def list_recoverable_prepared(self, **kwargs: object) -> list[_RecoveryTask]: ...

    def reconcile_prepared(self, task_id: uuid.UUID, **kwargs: object) -> bool: ...


class MarkdownCleaningOrchestrator:
    def __init__(
        self,
        *,
        repository: _Repository,
        resolver: _Resolver,
        input_validator: _InputValidator,
        processor: MarkdownCleaningProcessor,
        output_validator: _OutputValidator,
        publisher: _Publisher,
        staging_root: Path,
        max_output_bytes: int,
        lease_seconds: int,
        processing_timeout_seconds: int,
        lease_renewer: Callable[[uuid.UUID, str], bool],
        heartbeat_interval_seconds: float,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._repository = repository
        self._resolver = resolver
        self._input_validator = input_validator
        self._processor = processor
        self._output_validator = output_validator
        self._publisher = publisher
        self._staging_root = staging_root
        self._max_output_bytes = max_output_bytes
        self._lease_seconds = lease_seconds
        self._processing_timeout_seconds = processing_timeout_seconds
        self._lease_renewer = lease_renewer
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._clock = clock

    def execute(self, task_id: uuid.UUID) -> None:
        task = self._repository.acquire_queued(
            task_id,
            now=self._now(),
            lease_seconds=self._lease_seconds,
            processing_timeout_seconds=self._processing_timeout_seconds,
        )
        if task is None:
            return
        lease_token = task.lease_token
        if lease_token is None:
            raise LeaseLostError("claim 未返回租约 token")
        layout = StagingLayout.for_task(self._staging_root, task.id)
        terminal = False
        try:
            if task.processing_deadline is None:
                raise RetryableWorkerError("任务缺少处理 deadline")
            processing_deadline = task.processing_deadline
            if processing_deadline.tzinfo is None:
                processing_deadline = processing_deadline.replace(tzinfo=UTC)
            else:
                processing_deadline = processing_deadline.astimezone(UTC)
            if processing_deadline <= self._now():
                self._fail(task, lease_token, "PROCESSING_TIMEOUT", "处理超时")
                terminal = True
                return

            self._conditional(
                self._repository.update_progress(
                    task.id,
                    lease_token=lease_token,
                    progress_percent=15,
                    processing_phase="validating_input",
                    now=self._now(),
                )
            )
            resolved = self._resolver.resolve(task, layout)
            validated = self._input_validator.validate(
                resolved,
                layout,
                expected_processor_sha256=task.input_sha256,
            )
            self._conditional(
                self._repository.update_progress(
                    task.id,
                    lease_token=lease_token,
                    progress_percent=20,
                    processing_phase="cleaning",
                    now=self._now(),
                )
            )
            with LeaseHeartbeat(
                lambda: self._lease_renewer(task.id, lease_token),
                self._heartbeat_interval_seconds,
            ):
                result = self._processor.process(
                    validated.processor_path,
                    layout.result,
                    deadline=processing_deadline,
                )
            result = self._output_validator.validate(
                result,
                expected_input_sha256=validated.processor_sha256,
                max_output_bytes=self._max_output_bytes,
                expected_output_path=layout.result,
                source_path=validated.processor_path,
            )
            if processing_deadline <= self._now():
                self._fail(task, lease_token, "PROCESSING_TIMEOUT", "处理超时")
                terminal = True
                return
            prepared = self._publisher.prepare(layout.result)
            if prepared.sha256 != result.output_sha256:
                raise MarkdownCleaningProcessorError(
                    MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT,
                    "准备发布的摘要与处理结果不一致",
                )
            summary = result.summary
            self._conditional(
                self._repository.save_prepared(
                    task.id,
                    lease_token=lease_token,
                    staging_path=str(layout.result),
                    input_sha256=validated.processor_sha256,
                    prepared_output_sha256=prepared.sha256,
                    duplicate_paragraphs_removed=summary.duplicate_paragraphs_removed,
                    phone_redaction_count=summary.phone_redactions,
                    id_card_redaction_count=summary.id_card_redactions,
                    bank_card_redaction_count=summary.bank_card_redactions,
                    email_redaction_count=summary.email_redactions,
                    ipv4_redaction_count=summary.ipv4_redactions,
                    formatting_change_count=summary.formatting_changes,
                    progress_percent=85,
                    now=self._now(),
                )
            )
            self._conditional(
                self._repository.mark_publishing(
                    task.id, lease_token=lease_token, now=self._now()
                )
            )
            try:
                published = self._publisher.publish(
                    prepared, Path(task.target_path), allow_recovery=False
                )
            except OutputConflictError:
                self._fail(task, lease_token, "OUTPUT_CONFLICT", "输出目标冲突")
                terminal = True
                return
            except InvalidPreparedOutputError as exc:
                self._fail(
                    task, lease_token, "INVALID_PROCESSOR_OUTPUT", exc.safe_message
                )
                terminal = True
                return
            except PublicationSystemError as exc:
                raise RetryableWorkerError("发布临时失败") from exc
            try:
                self._conditional(
                    self._repository.mark_succeeded(
                        task.id,
                        lease_token=lease_token,
                        now=self._now(),
                        output_sha256=published.sha256,
                    )
                )
            except LeaseLostError:
                raise
            except Exception as exc:
                raise RetryableWorkerError("发布后状态写入失败") from exc
            terminal = True
        except LeaseLostError:
            raise
        except (MarkdownInputError, MarkdownCleaningProcessorError) as exc:
            code = exc.code.value
            self._fail(task, lease_token, code, exc.safe_message)
            terminal = True
        except Exception as exc:
            if task.attempt_count < task.max_attempts:
                raise RetryableWorkerError("临时系统错误") from exc
            self._fail(task, lease_token, "INTERNAL_ERROR", "内部处理错误")
            terminal = True
        finally:
            if terminal:
                layout.cleanup()

    def _fail(
        self,
        task: MarkdownCleaningWorkerTask,
        lease_token: str,
        error_code: str,
        safe_message: str,
    ) -> None:
        self._conditional(
            self._repository.mark_failed(
                task.id,
                lease_token=lease_token,
                now=self._now(),
                error_code=error_code,
                error_message=safe_message,
            )
        )

    @staticmethod
    def _conditional(updated: bool) -> None:
        if not updated:
            raise LeaseLostError("租约已失效或已被其他 worker 接管")

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock 必须返回 timezone-aware datetime")
        return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class RecoveryBatchResult:
    queued_errors: int = 0
    running_errors: int = 0
    prepared_errors: int = 0


class MarkdownCleaningRecovery:
    def __init__(
        self,
        *,
        repository: _RecoveryRepository,
        dispatcher: _Dispatcher,
        publisher: _Publisher,
        queue_recovery_interval_seconds: int,
        batch_size: int,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._repository = repository
        self._dispatcher = dispatcher
        self._publisher = publisher
        self._interval = queue_recovery_interval_seconds
        self._batch_size = batch_size
        self._clock = clock

    def recover_batch(self) -> RecoveryBatchResult:
        now = self._clock()
        queued_errors = self._recover_queued(now)
        running_errors = self._recover_running(now)
        prepared_errors = self._recover_prepared(now)
        return RecoveryBatchResult(queued_errors, running_errors, prepared_errors)

    def _recover_queued(self, now: datetime) -> int:
        errors = 0
        tasks = self._repository.list_recoverable_queued(
            now=now,
            queue_recovery_interval_seconds=self._interval,
            limit=self._batch_size,
        )
        for task in tasks:
            try:
                if self._repository.mark_recovery_dispatched(
                    task.id,
                    now=now,
                    queue_recovery_interval_seconds=self._interval,
                ):
                    self._dispatcher.enqueue_execute(task.id)
            except Exception:
                errors += 1
        return errors

    def _recover_running(self, now: datetime) -> int:
        errors = 0
        tasks = self._repository.list_recoverable_running(
            now=now, limit=self._batch_size
        )
        for task in tasks:
            try:
                if self._repository.recover_expired_running(
                    task.id, expected_lease_token=task.lease_token, now=now
                ):
                    self._dispatcher.enqueue_execute(task.id)
            except Exception:
                errors += 1
        return errors

    def _recover_prepared(self, now: datetime) -> int:
        errors = 0
        tasks = self._repository.list_recoverable_prepared(
            now=now, limit=self._batch_size
        )
        for task in tasks:
            try:
                source = Path(task.staging_path)
                prepared = PreparedMarkdownResult(
                    path=source,
                    sha256=task.prepared_output_sha256,
                    size_bytes=source.stat().st_size,
                )
                try:
                    published = self._publisher.publish(
                        prepared, Path(task.target_path), allow_recovery=True
                    )
                except OutputConflictError:
                    self._repository.reconcile_prepared(
                        task.id, now=now, outcome="output_conflict"
                    )
                    continue
                except InvalidPreparedOutputError:
                    self._repository.reconcile_prepared(
                        task.id, now=now, outcome="invalid_output"
                    )
                    continue
                except PublicationSystemError:
                    errors += 1
                    continue
                self._repository.reconcile_prepared(
                    task.id,
                    now=now,
                    outcome="succeeded",
                    output_sha256=published.sha256,
                )
            except Exception:
                errors += 1
        return errors
