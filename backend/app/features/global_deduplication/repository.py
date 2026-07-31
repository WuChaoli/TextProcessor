import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, update
from sqlalchemy.engine import CursorResult
from sqlmodel import Session, col, select

from app.features.global_deduplication.state_machine import (
    GlobalDeduplicationTaskStatus,
    assert_transition,
)
from app.features.global_deduplication.task_models import GlobalDeduplicationTask


class ConditionalGlobalDeduplicationUpdateFailed(RuntimeError):
    pass


class GlobalDeduplicationTaskRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, task_id: uuid.UUID) -> GlobalDeduplicationTask | None:
        return self._session.get(GlobalDeduplicationTask, task_id)

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
                        & (
                            col(GlobalDeduplicationTask.lease_expires_at)
                            .is_not(None)
                        )
                        & (
                            col(GlobalDeduplicationTask.lease_expires_at)
                            <= now
                        )
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
                        & (
                            col(GlobalDeduplicationTask.lease_expires_at)
                            .is_not(None)
                        )
                        & (
                            col(GlobalDeduplicationTask.lease_expires_at)
                            <= now
                        )
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

    def _execute_update(self, statement: Any) -> bool:
        result = self._session.exec(
            statement.execution_options(synchronize_session=False)
        )
        assert isinstance(result, CursorResult)
        changed = result.rowcount == 1
        self._session.commit()
        self._session.expire_all()
        return changed
