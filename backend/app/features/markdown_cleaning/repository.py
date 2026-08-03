import hashlib
import json
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Engine, and_, func, or_, update
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
from app.features.markdown_cleaning.worker_models import (
    MarkdownCleaningProcessingPhase,
)


class ConditionalMarkdownCleaningUpdateFailed(RuntimeError):
    pass


_local_lock_guard = threading.Lock()
_local_locks: dict[int, threading.Lock] = {}


def _require_aware_utc_datetime(value: datetime, *, field_name: str) -> None:
    if value.tzinfo is None:
        raise ValueError(f"{field_name} 必须包含时区信息")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} 必须是 UTC 时间")


def _recovery_due_before(
    now: datetime,
    *,
    queue_recovery_interval_seconds: int,
) -> datetime:
    return now - timedelta(seconds=queue_recovery_interval_seconds)


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
        if now is not None:
            _require_aware_utc_datetime(now, field_name="now")
        dispatched_at = now or get_datetime_utc()
        statement = (
            update(MarkdownCleaningTask)
            .where(
                col(MarkdownCleaningTask.id) == task_id,
                col(MarkdownCleaningTask.status) == MarkdownCleaningTaskStatus.QUEUED,
                col(MarkdownCleaningTask.last_dispatched_at).is_(None),
            )
            .values(last_dispatched_at=dispatched_at, updated_at=dispatched_at)
        )
        return self._execute_update(statement)

    def acquire_queued(
        self,
        task_id: uuid.UUID,
        *,
        now: datetime,
        lease_seconds: int,
    ) -> MarkdownCleaningTask | None:
        _require_aware_utc_datetime(now, field_name="now")
        token = str(uuid.uuid7())
        statement = (
            update(MarkdownCleaningTask)
            .where(
                col(MarkdownCleaningTask.id) == task_id,
                col(MarkdownCleaningTask.status) == MarkdownCleaningTaskStatus.QUEUED,
                col(MarkdownCleaningTask.attempt_count)
                < col(MarkdownCleaningTask.max_attempts),
            )
            .values(
                status=MarkdownCleaningTaskStatus.RUNNING,
                attempt_count=col(MarkdownCleaningTask.attempt_count) + 1,
                started_at=func.coalesce(col(MarkdownCleaningTask.started_at), now),
                lease_token=token,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                processing_phase=MarkdownCleaningProcessingPhase.CLAIMING_TASK,
                progress_percent=10,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if not self._execute_update(statement):
            return None
        task = self.get(task_id)
        if task is None:
            return None
        task.lease_token = token
        return task

    def renew_lease(
        self,
        task_id: uuid.UUID,
        *,
        lease_token: str,
        now: datetime,
        lease_seconds: int,
    ) -> bool:
        _require_aware_utc_datetime(now, field_name="now")
        statement = (
            update(MarkdownCleaningTask)
            .where(
                col(MarkdownCleaningTask.id) == task_id,
                col(MarkdownCleaningTask.status) == MarkdownCleaningTaskStatus.RUNNING,
                col(MarkdownCleaningTask.lease_token) == lease_token,
                col(MarkdownCleaningTask.lease_expires_at).is_not(None),
                col(MarkdownCleaningTask.lease_expires_at) > now,
            )
            .values(
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        return self._execute_update(statement)

    def update_progress(
        self,
        task_id: uuid.UUID,
        *,
        lease_token: str,
        progress_percent: int,
        processing_phase: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        values: dict[str, Any] = {"progress_percent": progress_percent}
        if processing_phase is not None:
            values["processing_phase"] = processing_phase
        return self._update_for_running_lease(task_id, lease_token, **values, now=now)

    def save_prepared(
        self,
        task_id: uuid.UUID,
        *,
        lease_token: str,
        staging_path: str,
        input_sha256: str,
        prepared_output_sha256: str,
        duplicate_paragraphs_removed: int | None = None,
        phone_redaction_count: int | None = None,
        id_card_redaction_count: int | None = None,
        bank_card_redaction_count: int | None = None,
        email_redaction_count: int | None = None,
        ipv4_redaction_count: int | None = None,
        formatting_change_count: int | None = None,
        progress_percent: int = 30,
        now: datetime | None = None,
    ) -> bool:
        return self._update_for_running_lease(
            task_id,
            lease_token,
            staging_path=staging_path,
            input_sha256=input_sha256,
            prepared_output_sha256=prepared_output_sha256,
            duplicate_paragraphs_removed=duplicate_paragraphs_removed,
            phone_redaction_count=phone_redaction_count,
            id_card_redaction_count=id_card_redaction_count,
            bank_card_redaction_count=bank_card_redaction_count,
            email_redaction_count=email_redaction_count,
            ipv4_redaction_count=ipv4_redaction_count,
            formatting_change_count=formatting_change_count,
            progress_percent=progress_percent,
            processing_phase=MarkdownCleaningProcessingPhase.SAVING_PREPARED,
            updated_at=now or get_datetime_utc(),
            now=now,
        )

    def mark_publishing(
        self,
        task_id: uuid.UUID,
        *,
        lease_token: str,
        now: datetime,
    ) -> bool:
        _require_aware_utc_datetime(now, field_name="now")
        return self._update_for_running_lease(
            task_id,
            lease_token,
            processing_phase=MarkdownCleaningProcessingPhase.PUBLISHING_RESULT,
            progress_percent=90,
            updated_at=now,
            now=now,
            clear_lease_token=False,
            require_prepared_artifacts=True,
            require_summary_counts=True,
            require_processing_phase=MarkdownCleaningProcessingPhase.SAVING_PREPARED,
        )

    def mark_succeeded(
        self,
        task_id: uuid.UUID,
        *,
        lease_token: str,
        now: datetime,
        output_sha256: str,
    ) -> bool:
        _require_aware_utc_datetime(now, field_name="now")
        return self._update_for_running_lease(
            task_id,
            lease_token,
            status=MarkdownCleaningTaskStatus.SUCCEEDED,
            processing_phase=MarkdownCleaningProcessingPhase.SUCCEEDED,
            progress_percent=100,
            output_sha256=output_sha256,
            finished_at=now,
            published_at=now,
            error_code=None,
            error_message=None,
            updated_at=now,
            now=now,
            clear_lease_token=True,
            require_summary_counts=True,
            require_prepared_artifacts=True,
            require_matching_output_sha256=output_sha256,
            require_processing_phase=MarkdownCleaningProcessingPhase.PUBLISHING_RESULT,
        )

    def mark_failed(
        self,
        task_id: uuid.UUID,
        *,
        lease_token: str,
        now: datetime,
        error_code: str,
        error_message: str,
        processing_phase: str = MarkdownCleaningProcessingPhase.FAILED,
    ) -> bool:
        _require_aware_utc_datetime(now, field_name="now")
        return self._update_for_running_lease(
            task_id,
            lease_token,
            status=MarkdownCleaningTaskStatus.FAILED,
            processing_phase=processing_phase,
            error_code=error_code,
            error_message=error_message,
            finished_at=now,
            updated_at=now,
            now=now,
            clear_lease_token=True,
        )

    def mark_recovery_dispatched(
        self,
        task_id: uuid.UUID,
        *,
        now: datetime,
        queue_recovery_interval_seconds: int,
    ) -> bool:
        if queue_recovery_interval_seconds <= 0:
            raise ValueError("queue_recovery_interval_seconds 必须大于 0")
        _require_aware_utc_datetime(now, field_name="now")
        recovery_before = _recovery_due_before(
            now, queue_recovery_interval_seconds=queue_recovery_interval_seconds
        )
        statement = (
            update(MarkdownCleaningTask)
            .where(
                col(MarkdownCleaningTask.id) == task_id,
                col(MarkdownCleaningTask.status) == MarkdownCleaningTaskStatus.QUEUED,
                col(MarkdownCleaningTask.attempt_count)
                < col(MarkdownCleaningTask.max_attempts),
                or_(
                    and_(
                        col(MarkdownCleaningTask.last_dispatched_at).is_(None),
                        col(MarkdownCleaningTask.queued_at) <= recovery_before,
                    ),
                    col(MarkdownCleaningTask.last_dispatched_at) <= recovery_before,
                ),
            )
            .values(last_dispatched_at=now, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        return self._execute_update(statement)

    def list_recoverable_queued(
        self,
        *,
        now: datetime,
        queue_recovery_interval_seconds: int,
        limit: int,
    ) -> list[MarkdownCleaningTask]:
        _require_aware_utc_datetime(now, field_name="now")
        if queue_recovery_interval_seconds <= 0:
            raise ValueError("queue_recovery_interval_seconds 必须大于 0")
        recovery_before = _recovery_due_before(
            now, queue_recovery_interval_seconds=queue_recovery_interval_seconds
        )
        statement = (
            select(MarkdownCleaningTask)
            .where(
                col(MarkdownCleaningTask.status) == MarkdownCleaningTaskStatus.QUEUED,
                col(MarkdownCleaningTask.attempt_count)
                < col(MarkdownCleaningTask.max_attempts),
                or_(
                    col(MarkdownCleaningTask.lease_token).is_(None),
                    col(MarkdownCleaningTask.lease_expires_at).is_(None),
                    col(MarkdownCleaningTask.lease_expires_at) <= now,
                ),
                or_(
                    and_(
                        col(MarkdownCleaningTask.last_dispatched_at).is_(None),
                        col(MarkdownCleaningTask.queued_at) <= recovery_before,
                    ),
                    col(MarkdownCleaningTask.last_dispatched_at) <= recovery_before,
                ),
            )
            .order_by(
                func.coalesce(
                    col(MarkdownCleaningTask.last_dispatched_at),
                    col(MarkdownCleaningTask.queued_at),
                ),
                col(MarkdownCleaningTask.id),
            )
            .limit(limit)
        )
        return list(self._session.exec(statement).all())

    def list_recoverable_running(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> list[MarkdownCleaningTask]:
        _require_aware_utc_datetime(now, field_name="now")
        statement = (
            select(MarkdownCleaningTask)
            .where(
                col(MarkdownCleaningTask.status) == MarkdownCleaningTaskStatus.RUNNING,
                col(MarkdownCleaningTask.lease_token).is_not(None),
                or_(
                    col(MarkdownCleaningTask.lease_expires_at).is_(None),
                    col(MarkdownCleaningTask.lease_expires_at) <= now,
                ),
            )
            .order_by(
                col(MarkdownCleaningTask.lease_expires_at),
                col(MarkdownCleaningTask.id),
            )
            .limit(limit)
        )
        return list(self._session.exec(statement).all())

    def count_active_running(self, *, now: datetime) -> int:
        _require_aware_utc_datetime(now, field_name="now")
        statement = (
            select(func.count())
            .select_from(MarkdownCleaningTask)
            .where(
                col(MarkdownCleaningTask.status) == MarkdownCleaningTaskStatus.RUNNING,
                col(MarkdownCleaningTask.lease_token).is_not(None),
                col(MarkdownCleaningTask.lease_expires_at) > now,
            )
        )
        return int(self._session.exec(statement).one())

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
        if not self._execute_update(statement):
            raise ConditionalMarkdownCleaningUpdateFailed(
                f"任务 {task_id} 当前状态不是 {expected}"
            )
        task = self._session.get(MarkdownCleaningTask, task_id)
        if task is None:
            raise ConditionalMarkdownCleaningUpdateFailed(f"任务 {task_id} 不存在")
        return task

    def _update_for_running_lease(
        self,
        task_id: uuid.UUID,
        lease_token: str,
        *,
        status: MarkdownCleaningTaskStatus | None = None,
        clear_lease_token: bool = False,
        now: datetime | None = None,
        require_prepared_artifacts: bool = False,
        require_summary_counts: bool = False,
        require_processing_phase: str | None = None,
        require_matching_output_sha256: str | None = None,
        **values: Any,
    ) -> bool:
        if status is not None:
            values["status"] = status
        if clear_lease_token:
            values["lease_token"] = None
            values["lease_expires_at"] = None
        now = now or get_datetime_utc()
        _require_aware_utc_datetime(now, field_name="now")
        values.setdefault("updated_at", now)

        conditions = [
            col(MarkdownCleaningTask.id) == task_id,
            col(MarkdownCleaningTask.status) == MarkdownCleaningTaskStatus.RUNNING,
            col(MarkdownCleaningTask.lease_token) == lease_token,
            col(MarkdownCleaningTask.lease_expires_at) > now,
        ]
        if require_prepared_artifacts:
            conditions.extend(
                (
                    col(MarkdownCleaningTask.staging_path).is_not(None),
                    col(MarkdownCleaningTask.input_sha256).is_not(None),
                    col(MarkdownCleaningTask.prepared_output_sha256).is_not(None),
                )
            )
        if require_summary_counts:
            conditions.extend(
                (
                    col(MarkdownCleaningTask.duplicate_paragraphs_removed).is_not(None),
                    col(MarkdownCleaningTask.phone_redaction_count).is_not(None),
                    col(MarkdownCleaningTask.id_card_redaction_count).is_not(None),
                    col(MarkdownCleaningTask.bank_card_redaction_count).is_not(None),
                    col(MarkdownCleaningTask.email_redaction_count).is_not(None),
                    col(MarkdownCleaningTask.ipv4_redaction_count).is_not(None),
                    col(MarkdownCleaningTask.formatting_change_count).is_not(None),
                )
            )
        if require_processing_phase is not None:
            conditions.append(
                col(MarkdownCleaningTask.processing_phase) == require_processing_phase
            )
        if require_matching_output_sha256 is not None:
            conditions.append(
                col(MarkdownCleaningTask.prepared_output_sha256)
                == require_matching_output_sha256
            )
        statement = (
            update(MarkdownCleaningTask)
            .where(*conditions)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        return self._execute_update(statement)

    def _execute_update(self, statement: Any) -> bool:
        result = self._session.exec(statement)
        assert isinstance(result, CursorResult)
        if result.rowcount != 1:
            self._session.rollback()
            return False
        self._session.commit()
        return True

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
