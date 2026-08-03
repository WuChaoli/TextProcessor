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

from app.features.markdown_cleaning.api_errors import (
    MarkdownCleaningApiErrorCode,
    MarkdownCleaningDomainError,
)
from app.features.markdown_cleaning.state_machine import (
    MarkdownCleaningTaskStatus,
    assert_transition,
)
from app.features.markdown_cleaning.task_models import (
    MarkdownCleaningTask,
    get_datetime_utc,
)


class ConditionalMarkdownCleaningUpdateFailed(RuntimeError):
    pass


_local_lock_guard = threading.Lock()
_local_locks: dict[int, threading.Lock] = {}


def request_fingerprint(
    *,
    file_storage_path: str | None,
    file_oss_url: str | None,
    selected_input_type: str,
    target_path: str,
) -> str:
    payload = {
        "file_storage_path": file_storage_path,
        "file_oss_url": file_oss_url,
        "selected_input_type": selected_input_type,
        "target_path": target_path,
    }
    normalized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(normalized.encode()).hexdigest()


def _idempotency_lock_key(caller_id: uuid.UUID, session_id: str, file_id: str) -> int:
    digest = hashlib.sha256(
        f"{caller_id}\0{session_id}\0{file_id}".encode()
    ).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


class MarkdownCleaningTaskRepository:
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
    ) -> tuple[MarkdownCleaningTask, bool]:
        with self.idempotency_lock(caller_id, session_id, file_id):
            fingerprint = request_fingerprint(
                file_storage_path=file_storage_path,
                file_oss_url=file_oss_url,
                selected_input_type=selected_input_type,
                target_path=target_path,
            )
            existing = self.get_by_key(caller_id, session_id, file_id)
            if existing is not None:
                self._ensure_same_request(existing, fingerprint)
                return existing, False

            task = MarkdownCleaningTask(
                caller_id=caller_id,
                session_id=session_id,
                file_id=file_id,
                request_fingerprint=fingerprint,
                file_storage_path=file_storage_path,
                file_oss_url=file_oss_url,
                selected_input_type=selected_input_type,
                target_path=target_path,
                status=MarkdownCleaningTaskStatus.PENDING,
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
    ) -> MarkdownCleaningTask | None:
        statement = select(MarkdownCleaningTask).where(
            MarkdownCleaningTask.caller_id == caller_id,
            MarkdownCleaningTask.session_id == session_id,
            MarkdownCleaningTask.file_id == file_id,
        )
        return self._session.exec(statement).first()

    def get(self, task_id: uuid.UUID) -> MarkdownCleaningTask | None:
        return self._session.get(MarkdownCleaningTask, task_id)

    def get_for_caller(
        self,
        task_id: uuid.UUID,
        caller_id: uuid.UUID,
    ) -> MarkdownCleaningTask | None:
        statement = select(MarkdownCleaningTask).where(
            MarkdownCleaningTask.id == task_id,
            MarkdownCleaningTask.caller_id == caller_id,
        )
        return self._session.exec(statement).first()

    def mark_dispatched(
        self,
        task_id: uuid.UUID,
        *,
        now: datetime | None = None,
    ) -> bool:
        dispatched_at = now or get_datetime_utc()
        statement = (
            update(MarkdownCleaningTask)
            .where(
                col(MarkdownCleaningTask.id) == task_id,
                col(MarkdownCleaningTask.status) == MarkdownCleaningTaskStatus.QUEUED,
            )
            .values(last_dispatched_at=dispatched_at, updated_at=dispatched_at)
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
        expected: MarkdownCleaningTaskStatus,
        target: MarkdownCleaningTaskStatus,
        **values: Any,
    ) -> MarkdownCleaningTask:
        assert_transition(expected, target)
        values.update(
            status=target,
            updated_at=get_datetime_utc(),
        )
        statement = (
            update(MarkdownCleaningTask)
            .where(
                col(MarkdownCleaningTask.id) == task_id,
                col(MarkdownCleaningTask.status) == expected,
            )
            .values(**values)
        )
        result = self._session.exec(statement)
        if not isinstance(result, CursorResult) or result.rowcount != 1:
            self._session.rollback()
            raise ConditionalMarkdownCleaningUpdateFailed(
                f"任务 {task_id} 当前状态不是 {expected}"
            )
        self._session.commit()
        task = self._session.get(MarkdownCleaningTask, task_id)
        if task is None:
            raise ConditionalMarkdownCleaningUpdateFailed(f"任务 {task_id} 不存在")
        return task

    @staticmethod
    def _ensure_same_request(
        task: MarkdownCleaningTask,
        fingerprint: str,
    ) -> None:
        if task.request_fingerprint != fingerprint:
            raise MarkdownCleaningDomainError(
                MarkdownCleaningApiErrorCode.IDEMPOTENCY_CONFLICT,
                "相同幂等键对应了不同请求参数",
                http_status=409,
            )
