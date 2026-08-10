import hashlib
import json
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Engine, func, or_, update
from sqlalchemy import select as sa_select
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from app.features.global_deduplication.api_errors import (
    GlobalDeduplicationApiErrorCode,
    GlobalDeduplicationDomainError,
)
from app.features.global_deduplication.state_machine import (
    GlobalDeduplicationTaskStatus,
    assert_transition,
)
from app.features.global_deduplication.task_models import GlobalDeduplicationTask


class ConditionalGlobalDeduplicationUpdateFailed(RuntimeError):
    pass


_local_lock_guard = threading.Lock()
_local_locks: dict[int, threading.Lock] = {}


def request_fingerprint(*, input_path: str) -> str:
    payload = {
        "input_path": input_path,
    }
    normalized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(normalized.encode()).hexdigest()


def _idempotency_lock_key(caller_id: uuid.UUID, session_id: str) -> int:
    digest = hashlib.sha256(f"{caller_id}\0{session_id}".encode()).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


class GlobalDeduplicationTaskRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, task_id: uuid.UUID) -> GlobalDeduplicationTask | None:
        return self._session.get(GlobalDeduplicationTask, task_id)

    def rollback(self) -> None:
        self._session.rollback()

    @contextmanager
    def idempotency_lock(
        self,
        caller_id: uuid.UUID,
        session_id: str,
    ) -> Iterator[None]:
        key = _idempotency_lock_key(caller_id, session_id)
        bind = self._session.get_bind()
        if isinstance(bind, Engine) and bind.dialect.name == "postgresql":
            with bind.connect() as connection:
                connection.execute(sa_select(func.pg_advisory_lock(key)))
                try:
                    yield
                finally:
                    connection.execute(sa_select(func.pg_advisory_unlock(key)))
            return
        with _local_lock_guard:
            lock = _local_locks.setdefault(key, threading.Lock())
        with lock:
            yield

    def create_or_get(
        self,
        *,
        caller_id: uuid.UUID,
        session_id: str,
        input_path: str,
    ) -> tuple[GlobalDeduplicationTask, bool]:
        fingerprint = request_fingerprint(
            input_path=input_path,
        )
        existing = self.get_by_key(caller_id, session_id)
        if existing is not None:
            self._ensure_same_request(existing, fingerprint)
            return existing, False
        task = GlobalDeduplicationTask(
            caller_id=caller_id,
            session_id=session_id,
            request_fingerprint=fingerprint,
            input_path=input_path,
            status=GlobalDeduplicationTaskStatus.PENDING,
        )
        self._session.add(task)
        try:
            self._session.commit()
        except IntegrityError:
            self._session.rollback()
            existing = self.get_by_key(caller_id, session_id)
            if existing is None:
                raise
            self._ensure_same_request(existing, fingerprint)
            return existing, False
        self._session.refresh(task)
        return task, True

    def get_by_key(
        self,
        caller_id: uuid.UUID,
        session_id: str,
    ) -> GlobalDeduplicationTask | None:
        statement = select(GlobalDeduplicationTask).where(
            GlobalDeduplicationTask.caller_id == caller_id,
            GlobalDeduplicationTask.session_id == session_id,
        )
        return self._session.exec(statement).first()

    def get_for_caller(
        self,
        task_id: uuid.UUID,
        caller_id: uuid.UUID,
    ) -> GlobalDeduplicationTask | None:
        statement = select(GlobalDeduplicationTask).where(
            GlobalDeduplicationTask.id == task_id,
            GlobalDeduplicationTask.caller_id == caller_id,
        )
        return self._session.exec(statement).first()

    def mark_dispatched(self, task_id: uuid.UUID, *, now: datetime) -> bool:
        statement = (
            update(GlobalDeduplicationTask)
            .where(
                col(GlobalDeduplicationTask.id) == task_id,
                col(GlobalDeduplicationTask.status)
                == GlobalDeduplicationTaskStatus.QUEUED,
            )
            .values(last_dispatched_at=now, updated_at=now)
        )
        return self._execute_update(statement)

    def acquire_submit(
        self,
        task_id: uuid.UUID,
        *,
        now: datetime,
        lease_seconds: int,
    ) -> GlobalDeduplicationTask | None:
        statement = (
            update(GlobalDeduplicationTask)
            .where(
                col(GlobalDeduplicationTask.id) == task_id,
                col(GlobalDeduplicationTask.attempt_count)
                < col(GlobalDeduplicationTask.max_attempts),
                or_(
                    col(GlobalDeduplicationTask.status)
                    == GlobalDeduplicationTaskStatus.QUEUED,
                    (
                        (
                            col(GlobalDeduplicationTask.status)
                            == GlobalDeduplicationTaskStatus.RUNNING
                        )
                        & col(GlobalDeduplicationTask.external_job_id).is_(None)
                        & (col(GlobalDeduplicationTask.lease_expires_at).is_not(None))
                        & (col(GlobalDeduplicationTask.lease_expires_at) <= now)
                    ),
                ),
            )
            .values(
                status=GlobalDeduplicationTaskStatus.RUNNING,
                attempt_count=col(GlobalDeduplicationTask.attempt_count) + 1,
                started_at=func.coalesce(
                    col(GlobalDeduplicationTask.started_at),
                    now,
                ),
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                processing_phase="validating_input",
                updated_at=now,
            )
        )
        if not self._execute_update(statement):
            return None
        return self.get(task_id)

    def acquire_poll(
        self,
        task_id: uuid.UUID,
        *,
        now: datetime,
        lease_seconds: int,
    ) -> GlobalDeduplicationTask | None:
        statement = (
            update(GlobalDeduplicationTask)
            .where(
                col(GlobalDeduplicationTask.id) == task_id,
                col(GlobalDeduplicationTask.status)
                == GlobalDeduplicationTaskStatus.RUNNING,
                col(GlobalDeduplicationTask.external_job_id).is_not(None),
                or_(
                    col(GlobalDeduplicationTask.next_poll_at).is_(None),
                    col(GlobalDeduplicationTask.next_poll_at) <= now,
                ),
                or_(
                    col(GlobalDeduplicationTask.poll_lease_expires_at).is_(None),
                    col(GlobalDeduplicationTask.poll_lease_expires_at) <= now,
                ),
            )
            .values(
                poll_lease_expires_at=now + timedelta(seconds=lease_seconds),
                updated_at=now,
            )
        )
        if not self._execute_update(statement):
            return None
        return self.get(task_id)

    def save_prepared_input(
        self,
        task_id: uuid.UUID,
        *,
        staging_path: str,
        input_manifest_sha256: str,
        input_jsonl_sha256: str,
        mapping_sha256: str,
        progress_total: int,
    ) -> bool:
        return self._update_running(
            task_id,
            staging_path=staging_path,
            input_manifest_sha256=input_manifest_sha256,
            input_jsonl_sha256=input_jsonl_sha256,
            mapping_sha256=mapping_sha256,
            progress_total=progress_total,
            progress_processed=progress_total,
            progress_percent=40,
            processing_phase="deduplicating",
        )

    def save_external_job(
        self,
        task_id: uuid.UUID,
        *,
        external_job_id: uuid.UUID,
        external_profile: str,
        next_poll_at: datetime,
        processing_deadline: datetime,
    ) -> bool:
        return self._update_running(
            task_id,
            external_job_id=external_job_id,
            external_profile=external_profile,
            external_status="queued",
            next_poll_at=next_poll_at,
            processing_deadline=processing_deadline,
            poll_lease_expires_at=None,
            lease_expires_at=None,
            processing_phase="deduplicating",
        )

    def save_prepared_output(
        self,
        task_id: uuid.UUID,
        *,
        external_output_sha256: str,
        prepared_output_sha256: str,
    ) -> bool:
        return self._update_running(
            task_id,
            external_output_sha256=external_output_sha256,
            prepared_output_sha256=prepared_output_sha256,
            processing_phase="publishing_result",
        )

    def update_running(self, task_id: uuid.UUID, **values: Any) -> bool:
        return self._update_running(task_id, **values)

    def mark_submission_uncertain(
        self,
        task_id: uuid.UUID,
        *,
        now: datetime,
        error_code: str,
        error_message: str,
    ) -> bool:
        return self._update_running(
            task_id,
            lease_expires_at=now,
            processing_phase="submitting",
            error_code=error_code,
            error_message=error_message,
            updated_at=now,
        )

    def transition(
        self,
        task_id: uuid.UUID,
        *,
        expected: GlobalDeduplicationTaskStatus,
        target: GlobalDeduplicationTaskStatus,
        **values: Any,
    ) -> GlobalDeduplicationTask:
        assert_transition(expected, target)
        values["status"] = target
        statement = (
            update(GlobalDeduplicationTask)
            .where(
                col(GlobalDeduplicationTask.id) == task_id,
                col(GlobalDeduplicationTask.status) == expected,
            )
            .values(**values)
        )
        if not self._execute_update(statement):
            raise ConditionalGlobalDeduplicationUpdateFailed
        task = self.get(task_id)
        if task is None:
            raise ConditionalGlobalDeduplicationUpdateFailed
        return task

    def list_due_polls(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> list[GlobalDeduplicationTask]:
        statement = (
            select(GlobalDeduplicationTask)
            .where(
                col(GlobalDeduplicationTask.status)
                == GlobalDeduplicationTaskStatus.RUNNING,
                col(GlobalDeduplicationTask.external_job_id).is_not(None),
                col(GlobalDeduplicationTask.next_poll_at).is_not(None),
                col(GlobalDeduplicationTask.next_poll_at) <= now,
                or_(
                    col(GlobalDeduplicationTask.poll_lease_expires_at).is_(None),
                    col(GlobalDeduplicationTask.poll_lease_expires_at) <= now,
                ),
            )
            .order_by(
                col(GlobalDeduplicationTask.next_poll_at),
                col(GlobalDeduplicationTask.id),
            )
            .limit(limit)
        )
        return list(self._session.exec(statement).all())

    def list_recoverable_submissions(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> list[GlobalDeduplicationTask]:
        statement = (
            select(GlobalDeduplicationTask)
            .where(
                col(GlobalDeduplicationTask.external_job_id).is_(None),
                or_(
                    col(GlobalDeduplicationTask.status)
                    == GlobalDeduplicationTaskStatus.QUEUED,
                    (
                        (
                            col(GlobalDeduplicationTask.status)
                            == GlobalDeduplicationTaskStatus.RUNNING
                        )
                        & (col(GlobalDeduplicationTask.lease_expires_at).is_not(None))
                        & (col(GlobalDeduplicationTask.lease_expires_at) <= now)
                    ),
                ),
            )
            .order_by(
                func.coalesce(
                    GlobalDeduplicationTask.lease_expires_at,
                    GlobalDeduplicationTask.queued_at,
                ),
                col(GlobalDeduplicationTask.id),
            )
            .limit(limit)
        )
        return list(self._session.exec(statement).all())

    def _update_running(self, task_id: uuid.UUID, **values: Any) -> bool:
        statement = (
            update(GlobalDeduplicationTask)
            .where(
                col(GlobalDeduplicationTask.id) == task_id,
                col(GlobalDeduplicationTask.status)
                == GlobalDeduplicationTaskStatus.RUNNING,
            )
            .values(**values)
        )
        return self._execute_update(statement)

    @staticmethod
    def _ensure_same_request(
        task: GlobalDeduplicationTask,
        fingerprint: str,
    ) -> None:
        if task.request_fingerprint != fingerprint:
            raise GlobalDeduplicationDomainError(
                GlobalDeduplicationApiErrorCode.IDEMPOTENCY_CONFLICT,
                "相同幂等键对应了不同请求参数",
                http_status=409,
            )

    def _execute_update(self, statement: Any) -> bool:
        result = self._session.exec(
            statement.execution_options(synchronize_session=False)
        )
        assert isinstance(result, CursorResult)
        changed = result.rowcount == 1
        self._session.commit()
        self._session.expire_all()
        return changed
