import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import update
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
        self._session.exec(update(ClassificationTask).where(col(ClassificationTask.id) == task_id, col(ClassificationTask.status) == TaskStatus.QUEUED).values(last_dispatched_at=utc_now(), updated_at=utc_now()))
        self._session.commit()
