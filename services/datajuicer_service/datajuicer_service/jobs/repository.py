import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.sql import ColumnElement, Executable

from datajuicer_service.jobs.models import DataJuicerJob
from datajuicer_service.jobs.state_machine import InvalidTransition, JobStatus


class IdempotencyConflict(ValueError):
    pass


class LeaseConflict(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class JobCreate:
    request_id: str
    profile: str
    input_path: str
    output_path: str
    max_attempts: int
    processing_deadline: datetime

    @property
    def fingerprint(self) -> str:
        payload = {
            "inputPath": self.input_path,
            "outputPath": self.output_path,
            "profile": self.profile,
        }
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class CreateJobResult:
    job: DataJuicerJob
    created: bool


@dataclass(frozen=True, slots=True)
class ExecutionLease:
    token: UUID
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class JobProgress:
    phase: str
    total: int | None
    processed: int
    percent: int


@dataclass(frozen=True, slots=True)
class JobResult:
    output_sha256: str
    published_at: datetime
    input_sha256: str
    input_count: int


@dataclass(frozen=True, slots=True)
class JobPrepared:
    output_sha256: str
    staging_output_path: str
    input_sha256: str
    input_count: int


@dataclass(frozen=True, slots=True)
class JobError:
    code: str
    message: str


class JobRepository:
    def __init__(
        self,
        session: Session,
        *,
        lease_seconds: int = 300,
        recovery_age_seconds: int = 30,
    ) -> None:
        self._session = session
        self._lease_seconds = lease_seconds
        self._recovery_age_seconds = recovery_age_seconds

    def create_or_get(
        self,
        request: JobCreate,
        *,
        now: datetime,
    ) -> CreateJobResult:
        job = DataJuicerJob(
            job_id=uuid4(),
            request_id=request.request_id,
            request_fingerprint=request.fingerprint,
            profile=request.profile,
            input_path=request.input_path,
            output_path=request.output_path,
            status=JobStatus.PENDING,
            processing_phase="pending",
            progress_total=None,
            progress_processed=0,
            progress_percent=0,
            attempt_count=0,
            max_attempts=request.max_attempts,
            lease_token=None,
            lease_expires_at=None,
            processing_deadline=request.processing_deadline,
            input_sha256=None,
            input_count=None,
            prepared_output_sha256=None,
            staging_output_path=None,
            output_sha256=None,
            published_at=None,
            error_code=None,
            error_message=None,
            created_at=now,
            queued_at=None,
            started_at=None,
            finished_at=None,
            updated_at=now,
        )
        self._session.add(job)
        try:
            self._session.commit()
        except IntegrityError as error:
            self._session.rollback()
            existing = self._session.scalar(
                select(DataJuicerJob).where(
                    DataJuicerJob.request_id == request.request_id
                )
            )
            if existing is None:
                raise error
            if existing.request_fingerprint != request.fingerprint:
                raise IdempotencyConflict("IDEMPOTENCY_CONFLICT")
            return CreateJobResult(job=existing, created=False)
        return CreateJobResult(job=job, created=True)

    def get(self, job_id: UUID) -> DataJuicerJob | None:
        return self._session.get(DataJuicerJob, job_id)

    def mark_queued(self, job_id: UUID, *, now: datetime) -> None:
        statement = (
            update(DataJuicerJob)
            .where(
                DataJuicerJob.job_id == job_id,
                DataJuicerJob.status == JobStatus.PENDING,
            )
            .values(
                status=JobStatus.QUEUED,
                processing_phase="queued",
                queued_at=now,
                updated_at=now,
            )
        )
        result = cast(CursorResult[Any], self._session.execute(statement))
        if result.rowcount != 1:
            self._session.rollback()
            raise InvalidTransition("INVALID_TRANSITION_TO_QUEUED")
        self._session.commit()

    def retry_failed_submission(self, job_id: UUID, *, now: datetime) -> bool:
        statement = (
            update(DataJuicerJob)
            .where(
                DataJuicerJob.job_id == job_id,
                DataJuicerJob.status == JobStatus.FAILED,
                DataJuicerJob.error_code == "QUEUE_SUBMISSION_FAILED",
            )
            .values(
                status=JobStatus.PENDING,
                processing_phase="pending",
                error_code=None,
                error_message=None,
                finished_at=None,
                updated_at=now,
            )
        )
        result = cast(CursorResult[Any], self._session.execute(statement))
        self._session.commit()
        return result.rowcount == 1

    def acquire_execution(
        self,
        job_id: UUID,
        *,
        now: datetime,
    ) -> ExecutionLease | None:
        token = uuid4()
        expires_at = now + timedelta(seconds=self._lease_seconds)
        executable_status = or_(
            DataJuicerJob.status == JobStatus.QUEUED,
            and_(
                DataJuicerJob.status == JobStatus.RUNNING,
                DataJuicerJob.lease_expires_at < now,
            ),
        )
        statement = (
            update(DataJuicerJob)
            .where(
                DataJuicerJob.job_id == job_id,
                executable_status,
                DataJuicerJob.attempt_count < DataJuicerJob.max_attempts,
                DataJuicerJob.processing_deadline > now,
            )
            .values(
                status=JobStatus.RUNNING,
                processing_phase="starting",
                attempt_count=DataJuicerJob.attempt_count + 1,
                lease_token=token,
                lease_expires_at=expires_at,
                started_at=func.coalesce(DataJuicerJob.started_at, now),
                updated_at=now,
            )
            .execution_options(synchronize_session="fetch")
        )
        result = cast(CursorResult[Any], self._session.execute(statement))
        if result.rowcount != 1:
            self._session.rollback()
            return None
        self._session.commit()
        return ExecutionLease(token=token, expires_at=expires_at)

    def update_progress(
        self,
        job_id: UUID,
        lease_token: UUID,
        progress: JobProgress,
        *,
        now: datetime,
    ) -> None:
        statement = (
            update(DataJuicerJob)
            .where(
                DataJuicerJob.job_id == job_id,
                DataJuicerJob.status == JobStatus.RUNNING,
                DataJuicerJob.lease_token == lease_token,
            )
            .values(
                processing_phase=progress.phase,
                progress_total=progress.total,
                progress_processed=progress.processed,
                progress_percent=progress.percent,
                lease_expires_at=now + timedelta(seconds=self._lease_seconds),
                updated_at=now,
            )
        )
        self._execute_with_lease(statement)

    def renew_lease(
        self,
        job_id: UUID,
        lease_token: UUID,
        *,
        now: datetime,
    ) -> None:
        statement = (
            update(DataJuicerJob)
            .where(
                DataJuicerJob.job_id == job_id,
                DataJuicerJob.status == JobStatus.RUNNING,
                DataJuicerJob.lease_token == lease_token,
            )
            .values(
                lease_expires_at=now + timedelta(seconds=self._lease_seconds),
                updated_at=now,
            )
        )
        self._execute_with_lease(statement)

    def mark_prepared(
        self,
        job_id: UUID,
        lease_token: UUID,
        prepared: JobPrepared,
        *,
        now: datetime,
    ) -> None:
        statement = (
            update(DataJuicerJob)
            .where(
                DataJuicerJob.job_id == job_id,
                DataJuicerJob.status == JobStatus.RUNNING,
                DataJuicerJob.lease_token == lease_token,
            )
            .values(
                processing_phase="publishing",
                input_sha256=prepared.input_sha256,
                input_count=prepared.input_count,
                prepared_output_sha256=prepared.output_sha256,
                staging_output_path=prepared.staging_output_path,
                lease_expires_at=now + timedelta(seconds=self._lease_seconds),
                updated_at=now,
            )
        )
        self._execute_with_lease(statement)

    def mark_succeeded(
        self,
        job_id: UUID,
        lease_token: UUID,
        result: JobResult,
        *,
        now: datetime,
    ) -> None:
        statement = (
            update(DataJuicerJob)
            .where(
                DataJuicerJob.job_id == job_id,
                DataJuicerJob.status == JobStatus.RUNNING,
                DataJuicerJob.lease_token == lease_token,
            )
            .values(
                status=JobStatus.SUCCEEDED,
                processing_phase="completed",
                progress_total=result.input_count,
                progress_processed=result.input_count,
                progress_percent=100,
                input_sha256=result.input_sha256,
                input_count=result.input_count,
                output_sha256=result.output_sha256,
                published_at=result.published_at,
                lease_token=None,
                lease_expires_at=None,
                finished_at=now,
                updated_at=now,
            )
        )
        self._execute_with_lease(statement)

    def expire_lease_for_retry(
        self,
        job_id: UUID,
        lease_token: UUID,
        *,
        now: datetime,
    ) -> None:
        statement = (
            update(DataJuicerJob)
            .where(
                DataJuicerJob.job_id == job_id,
                DataJuicerJob.status == JobStatus.RUNNING,
                DataJuicerJob.lease_token == lease_token,
            )
            .values(lease_expires_at=now, updated_at=now)
        )
        self._execute_with_lease(statement)

    def touch_recovery_dispatch(self, job_id: UUID, *, now: datetime) -> None:
        statement = (
            update(DataJuicerJob)
            .where(
                DataJuicerJob.job_id == job_id,
                DataJuicerJob.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
            )
            .values(updated_at=now)
        )
        result = cast(CursorResult[Any], self._session.execute(statement))
        if result.rowcount != 1:
            self._session.rollback()
            raise InvalidTransition("RECOVERY_DISPATCH_CONFLICT")
        self._session.commit()

    def mark_failed(
        self,
        job_id: UUID,
        lease_token: UUID | None,
        error: JobError,
        *,
        now: datetime,
    ) -> None:
        if lease_token is None:
            ownership: ColumnElement[bool] = DataJuicerJob.status.in_(
                [JobStatus.PENDING, JobStatus.QUEUED]
            )
        else:
            ownership = and_(
                DataJuicerJob.status == JobStatus.RUNNING,
                DataJuicerJob.lease_token == lease_token,
            )
        statement = (
            update(DataJuicerJob)
            .where(DataJuicerJob.job_id == job_id, ownership)
            .values(
                status=JobStatus.FAILED,
                processing_phase="failed",
                error_code=error.code,
                error_message=error.message,
                lease_token=None,
                lease_expires_at=None,
                finished_at=now,
                updated_at=now,
            )
        )
        self._execute_with_lease(statement)

    def mark_timed_out(self, job_id: UUID, *, now: datetime) -> None:
        ownership = or_(
            DataJuicerJob.status.in_([JobStatus.PENDING, JobStatus.QUEUED]),
            and_(
                DataJuicerJob.status == JobStatus.RUNNING,
                DataJuicerJob.lease_expires_at < now,
            ),
        )
        statement = (
            update(DataJuicerJob)
            .where(DataJuicerJob.job_id == job_id, ownership)
            .values(
                status=JobStatus.FAILED,
                processing_phase="failed",
                error_code="JOB_TIMEOUT",
                error_message="任务执行超时",
                lease_token=None,
                lease_expires_at=None,
                finished_at=now,
                updated_at=now,
            )
        )
        self._execute_with_lease(statement)

    def find_recoverable(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> list[DataJuicerJob]:
        stale_before = now - timedelta(seconds=self._recovery_age_seconds)
        statement = (
            select(DataJuicerJob)
            .where(
                or_(
                    and_(
                        DataJuicerJob.status.in_([JobStatus.PENDING, JobStatus.QUEUED]),
                        DataJuicerJob.updated_at <= stale_before,
                    ),
                    and_(
                        DataJuicerJob.status == JobStatus.RUNNING,
                        DataJuicerJob.lease_expires_at < now,
                        DataJuicerJob.updated_at <= stale_before,
                    ),
                )
            )
            .order_by(DataJuicerJob.created_at, DataJuicerJob.job_id)
            .limit(limit)
        )
        return list(self._session.scalars(statement))

    def _execute_with_lease(self, statement: Executable) -> None:
        result = cast(CursorResult[Any], self._session.execute(statement))
        if result.rowcount != 1:
            self._session.rollback()
            raise LeaseConflict("LEASE_CONFLICT")
        self._session.commit()
