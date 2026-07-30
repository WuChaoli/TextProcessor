import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import update
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
        result = self._session.execute(statement)
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
