from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread
from typing import Protocol
from uuid import UUID

from datajuicer_service.jobs.dispatcher import ExecutionMessage, JobDispatcher
from datajuicer_service.jobs.models import DataJuicerJob
from datajuicer_service.jobs.repository import (
    ExecutionLease,
    JobError,
    JobPrepared,
    JobProgress,
    JobResult,
)
from datajuicer_service.jobs.state_machine import TERMINAL_STATUSES, JobStatus
from datajuicer_service.profiles.io import OutputValidationError, ProfileInputError
from datajuicer_service.profiles.registry import UnknownProfileError
from datajuicer_service.profiles.text_exact_minhash_v1 import (
    OutputConflictError,
    PreparedCallback,
    ProfileResult,
    ProgressCallback,
    publish_prepared_output,
    sha256_file,
)


class ProfileExecutor(Protocol):
    def execute(
        self,
        input_path: Path,
        output_path: Path,
        *,
        request_id: str,
        progress: ProgressCallback | None = None,
        prepared: PreparedCallback | None = None,
    ) -> ProfileResult: ...


class OrchestrationRepository(Protocol):
    def get(self, job_id: UUID) -> DataJuicerJob | None: ...

    def acquire_execution(
        self,
        job_id: UUID,
        *,
        now: datetime,
    ) -> ExecutionLease | None: ...

    def update_progress(
        self,
        job_id: UUID,
        lease_token: UUID,
        progress: JobProgress,
        *,
        now: datetime,
    ) -> None: ...

    def renew_lease(
        self,
        job_id: UUID,
        lease_token: UUID,
        *,
        now: datetime,
    ) -> None: ...

    def mark_prepared(
        self,
        job_id: UUID,
        lease_token: UUID,
        prepared: JobPrepared,
        *,
        now: datetime,
    ) -> None: ...

    def mark_succeeded(
        self,
        job_id: UUID,
        lease_token: UUID,
        result: JobResult,
        *,
        now: datetime,
    ) -> None: ...

    def mark_failed(
        self,
        job_id: UUID,
        lease_token: UUID | None,
        error: JobError,
        *,
        now: datetime,
    ) -> None: ...

    def mark_timed_out(self, job_id: UUID, *, now: datetime) -> None: ...

    def expire_lease_for_retry(
        self,
        job_id: UUID,
        lease_token: UUID,
        *,
        now: datetime,
    ) -> None: ...

    def find_recoverable(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> list[DataJuicerJob]: ...

    def mark_queued(self, job_id: UUID, *, now: datetime) -> None: ...

    def touch_recovery_dispatch(self, job_id: UUID, *, now: datetime) -> None: ...


RepositoryFactory = Callable[
    [],
    AbstractContextManager[OrchestrationRepository],
]
ProfileResolver = Callable[[str], ProfileExecutor]


class RetryableJobError(RuntimeError):
    pass


class JobTimeoutError(TimeoutError):
    pass


PHASE_PERCENT = {
    "validating_input": 5,
    "exact_grouping": 20,
    "minhash_computing": 50,
    "minhash_clustering": 70,
    "expanding_clusters": 80,
    "writing_result": 90,
}


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class JobOrchestrator:
    def __init__(
        self,
        *,
        repository_factory: RepositoryFactory,
        profile_resolver: ProfileResolver,
        now: Callable[[], datetime],
        dispatcher: JobDispatcher | None = None,
        recovery_batch_size: int = 100,
        lease_heartbeat_seconds: float = 30,
    ) -> None:
        self._repository_factory = repository_factory
        self._profile_resolver = profile_resolver
        self._now = now
        self._dispatcher = dispatcher
        self._recovery_batch_size = recovery_batch_size
        self._lease_heartbeat_seconds = lease_heartbeat_seconds

    def execute(self, job_id: UUID) -> None:
        with self._repository_factory() as repository:
            job = repository.get(job_id)
            if job is None or job.status in TERMINAL_STATUSES:
                return
            now = self._now()
            if _as_utc(now) >= _as_utc(job.processing_deadline):
                repository.mark_timed_out(job_id, now=now)
                return
            lease = repository.acquire_execution(job_id, now=now)
            if lease is None:
                return
            job = repository.get(job_id)
            if job is None:
                return
            try:
                if self._recover_prepared(repository, job, lease):
                    return
                output_path = Path(job.output_path)
                if output_path.exists():
                    raise OutputConflictError("OUTPUT_CONFLICT")
                profile = self._profile_resolver(job.profile)
                with self._lease_heartbeat(job.job_id, lease):
                    result = profile.execute(
                        Path(job.input_path),
                        output_path,
                        request_id=job.request_id,
                        progress=self._progress_callback(repository, job, lease),
                        prepared=self._prepared_callback(repository, job, lease),
                    )
                repository.mark_succeeded(
                    job.job_id,
                    lease.token,
                    JobResult(
                        output_sha256=result.output_sha256,
                        published_at=self._now(),
                        input_sha256=result.input_sha256,
                        input_count=result.input_count,
                    ),
                    now=self._now(),
                )
            except FileNotFoundError:
                self._fail(repository, job, lease, "INPUT_NOT_FOUND", "输入文件不存在")
            except ProfileInputError as error:
                code = self._map_input_error(error.code)
                self._fail(repository, job, lease, code, "输入数据格式不正确")
            except OutputConflictError:
                self._fail(repository, job, lease, "OUTPUT_CONFLICT", "输出路径冲突")
            except OutputValidationError:
                self._fail(
                    repository,
                    job,
                    lease,
                    "INVALID_PROFILE_OUTPUT",
                    "处理结果校验失败",
                )
            except UnknownProfileError:
                self._fail(
                    repository,
                    job,
                    lease,
                    "PROFILE_NOT_SUPPORTED",
                    "不支持的处理配置",
                )
            except JobTimeoutError:
                self._fail(repository, job, lease, "JOB_TIMEOUT", "任务执行超时")
            except OSError as error:
                refreshed = repository.get(job.job_id)
                if (
                    refreshed is not None
                    and refreshed.attempt_count >= refreshed.max_attempts
                ):
                    self._fail(
                        repository,
                        refreshed,
                        lease,
                        "PROFILE_EXECUTION_FAILED",
                        "处理执行失败",
                    )
                    return
                repository.expire_lease_for_retry(
                    job.job_id,
                    lease.token,
                    now=self._now(),
                )
                raise RetryableJobError("RETRYABLE_INFRASTRUCTURE_ERROR") from error
            except Exception:
                self._fail(
                    repository,
                    job,
                    lease,
                    "PROFILE_EXECUTION_FAILED",
                    "处理执行失败",
                )

    def recover(self) -> int:
        if self._dispatcher is None:
            raise RuntimeError("RECOVERY_DISPATCHER_NOT_CONFIGURED")
        now = self._now()
        with self._repository_factory() as repository:
            jobs = repository.find_recoverable(
                now=now,
                limit=self._recovery_batch_size,
            )
            dispatched = 0
            for job in jobs:
                self._dispatcher.enqueue(ExecutionMessage(job_id=job.job_id))
                if job.status is JobStatus.PENDING:
                    repository.mark_queued(job.job_id, now=now)
                else:
                    repository.touch_recovery_dispatch(job.job_id, now=now)
                dispatched += 1
            return dispatched

    @contextmanager
    def _lease_heartbeat(
        self,
        job_id: UUID,
        lease: ExecutionLease,
    ) -> Iterator[None]:
        stopped = Event()
        errors: list[Exception] = []

        def renew() -> None:
            while not stopped.wait(self._lease_heartbeat_seconds):
                try:
                    with self._repository_factory() as repository:
                        repository.renew_lease(
                            job_id,
                            lease.token,
                            now=self._now(),
                        )
                except Exception as error:
                    errors.append(error)
                    stopped.set()

        thread = Thread(
            target=renew,
            name=f"datajuicer-lease-{job_id}",
            daemon=True,
        )
        thread.start()
        try:
            yield
        finally:
            stopped.set()
            thread.join(timeout=self._lease_heartbeat_seconds + 1)
        if errors:
            raise errors[0]

    def _recover_prepared(
        self,
        repository: OrchestrationRepository,
        job: DataJuicerJob,
        lease: ExecutionLease,
    ) -> bool:
        if job.prepared_output_sha256 is None:
            return False
        output_path = Path(job.output_path)
        if output_path.exists():
            if sha256_file(output_path) != job.prepared_output_sha256:
                raise OutputConflictError("OUTPUT_CONFLICT")
        elif (
            job.staging_output_path is not None
            and Path(job.staging_output_path).exists()
        ):
            publish_prepared_output(
                Path(job.staging_output_path),
                output_path,
                job.prepared_output_sha256,
            )
        else:
            return False
        if job.input_sha256 is None or job.input_count is None:
            raise OutputValidationError("MISSING_PREPARED_INPUT_METADATA")
        repository.mark_succeeded(
            job.job_id,
            lease.token,
            JobResult(
                output_sha256=job.prepared_output_sha256,
                published_at=self._now(),
                input_sha256=job.input_sha256,
                input_count=job.input_count,
            ),
            now=self._now(),
        )
        return True

    def _progress_callback(
        self,
        repository: OrchestrationRepository,
        job: DataJuicerJob,
        lease: ExecutionLease,
    ) -> ProgressCallback:
        last_percent = 0

        def report(phase: str, processed: int, total: int | None) -> None:
            nonlocal last_percent
            now = self._now()
            if _as_utc(now) >= _as_utc(job.processing_deadline):
                raise JobTimeoutError
            percent = max(last_percent, PHASE_PERCENT.get(phase, last_percent))
            last_percent = percent
            repository.update_progress(
                job.job_id,
                lease.token,
                JobProgress(
                    phase=phase,
                    total=total,
                    processed=processed,
                    percent=percent,
                ),
                now=now,
            )

        return report

    def _prepared_callback(
        self,
        repository: OrchestrationRepository,
        job: DataJuicerJob,
        lease: ExecutionLease,
    ) -> PreparedCallback:
        def record(
            staging_path: Path,
            output_sha256: str,
            input_sha256: str,
            input_count: int,
        ) -> None:
            repository.mark_prepared(
                job.job_id,
                lease.token,
                JobPrepared(
                    output_sha256=output_sha256,
                    staging_output_path=str(staging_path),
                    input_sha256=input_sha256,
                    input_count=input_count,
                ),
                now=self._now(),
            )

        return record

    def _fail(
        self,
        repository: OrchestrationRepository,
        job: DataJuicerJob,
        lease: ExecutionLease,
        code: str,
        message: str,
    ) -> None:
        repository.mark_failed(
            job.job_id,
            lease.token,
            JobError(code=code, message=message),
            now=self._now(),
        )

    @staticmethod
    def _map_input_error(code: str) -> str:
        if code == "DUPLICATE_UID":
            return code
        if code == "EMPTY_INPUT":
            return "EMPTY_INPUT_DATASET"
        if code in {"MAX_RECORDS", "MAX_BYTES", "MAX_TEXT_CHARS"}:
            return "INPUT_TOO_LARGE"
        if code == "INPUT_UNREADABLE":
            return "INPUT_READ_FAILED"
        return "INVALID_INPUT_DATASET"
