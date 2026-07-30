import hashlib
import json
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from sqlalchemy import Engine, func, update
from sqlalchemy import select as sa_select
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from app.features.structured_extraction.errors import (
    ExtractionDomainError,
    ExtractionErrorCode,
)
from app.features.structured_extraction.models import (
    ExtractionTask,
    ExtractionTaskStatus,
    get_datetime_utc,
)
from app.features.structured_extraction.state_machine import assert_transition


class ConditionalTransitionFailed(RuntimeError):
    pass


_local_lock_guard = threading.Lock()
_local_locks: dict[int, threading.Lock] = {}


def _idempotency_lock_key(
    caller_id: uuid.UUID,
    session_id: str,
    file_id: str,
) -> int:
    digest = hashlib.sha256(f"{caller_id}\0{session_id}\0{file_id}".encode()).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


def request_fingerprint(
    *,
    session_id: str,
    file_id: str,
    file_storage_path: str | None,
    file_oss_url: str | None,
    selected_input_type: str,
    target_path: str,
) -> str:
    payload = {
        "file_id": file_id,
        "file_oss_url": file_oss_url,
        "file_storage_path": file_storage_path,
        "selected_input_type": selected_input_type,
        "session_id": session_id,
        "target_path": target_path,
    }
    normalized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(normalized.encode()).hexdigest()


class ExtractionTaskRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def rollback(self) -> None:
        self._session.rollback()

    @contextmanager
    def idempotency_lock(
        self,
        caller_id: uuid.UUID,
        session_id: str,
        file_id: str,
    ) -> Iterator[None]:
        key = _idempotency_lock_key(caller_id, session_id, file_id)
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
        file_id: str,
        file_storage_path: str | None,
        file_oss_url: str | None,
        selected_input_type: str,
        target_path: str,
    ) -> tuple[ExtractionTask, bool]:
        fingerprint = request_fingerprint(
            session_id=session_id,
            file_id=file_id,
            file_storage_path=file_storage_path,
            file_oss_url=file_oss_url,
            selected_input_type=selected_input_type,
            target_path=target_path,
        )
        existing = self.get_by_key(caller_id, session_id, file_id)
        if existing is not None:
            self._ensure_same_request(existing, fingerprint)
            return existing, False

        task = ExtractionTask(
            caller_id=caller_id,
            session_id=session_id,
            file_id=file_id,
            request_fingerprint=fingerprint,
            file_storage_path=file_storage_path,
            file_oss_url=file_oss_url,
            selected_input_type=selected_input_type,
            target_path=target_path,
            status=ExtractionTaskStatus.PENDING,
        )
        self._session.add(task)
        try:
            self._session.commit()
        except IntegrityError:
            self._session.rollback()
            existing = self.get_by_key(caller_id, session_id, file_id)
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
        file_id: str,
    ) -> ExtractionTask | None:
        statement = select(ExtractionTask).where(
            ExtractionTask.caller_id == caller_id,
            ExtractionTask.session_id == session_id,
            ExtractionTask.file_id == file_id,
        )
        return self._session.exec(statement).first()

    def get_for_caller(
        self,
        task_id: uuid.UUID,
        caller_id: uuid.UUID,
    ) -> ExtractionTask | None:
        statement = select(ExtractionTask).where(
            ExtractionTask.id == task_id,
            ExtractionTask.caller_id == caller_id,
        )
        return self._session.exec(statement).first()

    def list_undispatched_queued(
        self,
        *,
        queued_before: datetime,
        limit: int = 100,
    ) -> list[ExtractionTask]:
        statement = (
            select(ExtractionTask)
            .where(
                ExtractionTask.status == ExtractionTaskStatus.QUEUED,
                col(ExtractionTask.last_dispatched_at).is_(None),
                col(ExtractionTask.queued_at).is_not(None),
                col(ExtractionTask.queued_at) <= queued_before,
            )
            .limit(limit)
        )
        return list(self._session.exec(statement).all())

    def mark_dispatched(
        self,
        task_id: uuid.UUID,
        *,
        dispatched_at: datetime | None = None,
    ) -> bool:
        statement = (
            update(ExtractionTask)
            .where(
                col(ExtractionTask.id) == task_id,
                col(ExtractionTask.status) == ExtractionTaskStatus.QUEUED,
                col(ExtractionTask.last_dispatched_at).is_(None),
            )
            .values(
                last_dispatched_at=dispatched_at or get_datetime_utc(),
                updated_at=get_datetime_utc(),
            )
        )
        result = self._session.exec(statement)
        if not isinstance(result, CursorResult) or result.rowcount != 1:
            self._session.rollback()
            return False
        self._session.commit()
        return True

    def transition(
        self,
        task_id: uuid.UUID,
        *,
        expected: ExtractionTaskStatus,
        target: ExtractionTaskStatus,
        **fields: Any,
    ) -> ExtractionTask:
        assert_transition(expected, target)
        values = {
            "status": target,
            "updated_at": get_datetime_utc(),
            **fields,
        }
        statement = (
            update(ExtractionTask)
            .where(
                col(ExtractionTask.id) == task_id,
                col(ExtractionTask.status) == expected,
            )
            .values(**values)
        )
        result = self._session.exec(statement)
        if not isinstance(result, CursorResult) or result.rowcount != 1:
            self._session.rollback()
            raise ConditionalTransitionFailed(f"任务 {task_id} 当前状态不是 {expected}")
        self._session.commit()
        task = self._session.get(ExtractionTask, task_id)
        if task is None:
            raise ConditionalTransitionFailed(f"任务 {task_id} 不存在")
        return task

    @staticmethod
    def _ensure_same_request(task: ExtractionTask, fingerprint: str) -> None:
        if task.request_fingerprint != fingerprint:
            raise ExtractionDomainError(
                ExtractionErrorCode.IDEMPOTENCY_CONFLICT,
                "相同幂等键对应了不同请求参数",
                http_status=409,
            )
