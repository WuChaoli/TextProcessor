import hashlib
import json
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, update
from sqlalchemy.engine import CursorResult
from sqlmodel import Session, col, select

from app.features.text_classification.models import ClassificationTask, utc_now
from app.tasking.state import TaskStatus, ensure_transition


def fingerprint(session_id: str, file_id: str, input_uri: str) -> str:
    value = json.dumps({"file_id": file_id, "input_uri": input_uri, "session_id": session_id}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(value.encode()).hexdigest()


class ClassificationTaskRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create_or_get(self, *, caller_id: uuid.UUID, session_id: str, file_id: str, input_uri: str) -> tuple[ClassificationTask, bool]:
        existing = self.get_by_key(caller_id, session_id, file_id)
        request_fingerprint = fingerprint(session_id, file_id, input_uri)
        if existing:
            if existing.request_fingerprint != request_fingerprint:
                raise ValueError("IDEMPOTENCY_CONFLICT")
            return existing, False
        task = ClassificationTask(caller_id=caller_id, session_id=session_id, file_id=file_id, input_uri=input_uri, request_fingerprint=request_fingerprint, status=TaskStatus.PENDING)
        self._session.add(task)
        self._session.commit()
        self._session.refresh(task)
        return task, True

    def get_by_key(self, caller_id: uuid.UUID, session_id: str, file_id: str) -> ClassificationTask | None:
        return self._session.exec(select(ClassificationTask).where(ClassificationTask.caller_id == caller_id, ClassificationTask.session_id == session_id, ClassificationTask.file_id == file_id)).first()

    def get_for_caller(self, task_id: uuid.UUID, caller_id: uuid.UUID) -> ClassificationTask | None:
        return self._session.exec(select(ClassificationTask).where(ClassificationTask.id == task_id, ClassificationTask.caller_id == caller_id)).first()

    def get(self, task_id: uuid.UUID) -> ClassificationTask | None:
        return self._session.get(ClassificationTask, task_id)

    def claim_for_execution(
        self,
        task_id: uuid.UUID,
        *,
        now: datetime,
        lease_seconds: int,
    ) -> ClassificationTask | None:
        result = self._session.exec(
            update(ClassificationTask)
            .where(
                col(ClassificationTask.id) == task_id,
                col(ClassificationTask.attempt_count) < col(ClassificationTask.max_attempts),
                or_(
                    col(ClassificationTask.status) == TaskStatus.QUEUED,
                    (
                        (col(ClassificationTask.status) == TaskStatus.RUNNING)
                        & (col(ClassificationTask.lease_expires_at) <= now)
                    ),
                ),
            )
            .values(
                status=TaskStatus.RUNNING,
                attempt_count=ClassificationTask.attempt_count + 1,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                started_at=func.coalesce(ClassificationTask.started_at, now),
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if not isinstance(result, CursorResult) or result.rowcount != 1:
            self._session.rollback()
            return None
        self._session.commit()
        return self.get(task_id)

    def list_recoverable(self, *, now: datetime, limit: int) -> list[ClassificationTask]:
        statement = (
            select(ClassificationTask)
            .where(
                or_(
                    (col(ClassificationTask.status) == TaskStatus.QUEUED)
                    & col(ClassificationTask.last_dispatched_at).is_(None),
                    (col(ClassificationTask.status) == TaskStatus.RUNNING)
                    & (col(ClassificationTask.lease_expires_at) <= now),
                )
            )
            .order_by(col(ClassificationTask.created_at), col(ClassificationTask.id))
            .limit(limit)
        )
        return list(self._session.exec(statement).all())

    def transition(self, task_id: uuid.UUID, *, expected: TaskStatus, target: TaskStatus, **fields: Any) -> ClassificationTask:
        ensure_transition(expected, target)
        result = self._session.exec(update(ClassificationTask).where(col(ClassificationTask.id) == task_id, col(ClassificationTask.status) == expected).values(status=target, updated_at=utc_now(), **fields))
        if not isinstance(result, CursorResult) or result.rowcount != 1:
            self._session.rollback()
            raise RuntimeError("CONDITIONAL_TRANSITION_FAILED")
        self._session.commit()
        task = self.get(task_id)
        if task is None:
            raise RuntimeError("TASK_NOT_FOUND")
        return task

    def mark_dispatched(self, task_id: uuid.UUID) -> None:
        self._session.exec(update(ClassificationTask).where(col(ClassificationTask.id) == task_id, col(ClassificationTask.status).in_((TaskStatus.QUEUED, TaskStatus.RUNNING))).values(last_dispatched_at=utc_now(), updated_at=utc_now()))
        self._session.commit()

    def update_running(self, task_id: uuid.UUID, **fields: Any) -> bool:
        result = self._session.exec(
            update(ClassificationTask)
            .where(
                col(ClassificationTask.id) == task_id,
                col(ClassificationTask.status) == TaskStatus.RUNNING,
            )
            .values(updated_at=utc_now(), **fields)
            .execution_options(synchronize_session=False)
        )
        if not isinstance(result, CursorResult) or result.rowcount != 1:
            self._session.rollback()
            return False
        self._session.commit()
        return True
