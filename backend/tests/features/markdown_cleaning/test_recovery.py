from __future__ import annotations

import uuid
from collections.abc import Generator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.features.markdown_cleaning.orchestration import MarkdownCleaningRecovery
from app.features.markdown_cleaning.processors.errors import MarkdownCleaningErrorCode
from app.features.markdown_cleaning.publisher import (
    InvalidPreparedOutputError,
    OutputConflictError,
    PublicationSystemError,
)
from app.features.markdown_cleaning.repository import MarkdownCleaningTaskRepository
from app.features.markdown_cleaning.state_machine import MarkdownCleaningTaskStatus
from app.features.markdown_cleaning.task_models import MarkdownCleaningTask
from app.models import User

NOW = datetime(2026, 8, 3, 8, tzinfo=UTC)


@pytest.fixture(scope="session", autouse=True)
def db() -> None:
    """Override the backend-wide PostgreSQL fixture for this pure unit module."""


@pytest.fixture
def session() -> Generator[Session]:
    _ = User
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as value:
        yield value


@dataclass
class FakeRecoveryRepository:
    queued: list[Any]
    running: list[Any]
    prepared: list[Any]

    def __post_init__(self) -> None:
        self.recovered: list[tuple[uuid.UUID, str, str | None]] = []

    def list_recoverable_queued(self, **kwargs: Any) -> list[Any]:
        return self.queued

    def list_recoverable_running(self, **kwargs: Any) -> list[Any]:
        return self.running

    def list_recoverable_prepared(self, **kwargs: Any) -> list[Any]:
        return self.prepared

    def recover_expired_running(self, task_id: uuid.UUID, **kwargs: Any) -> bool:
        self.recovered.append((task_id, "running", None))
        return True

    def reconcile_prepared(self, task_id: uuid.UUID, **kwargs: Any) -> bool:
        self.recovered.append((task_id, kwargs["outcome"], kwargs.get("output_sha256")))
        return True

    def mark_recovery_dispatched(self, task_id: uuid.UUID, **kwargs: Any) -> bool:
        self.recovered.append((task_id, "queued", None))
        return True


class FakeDispatcher:
    def __init__(self, fail_for: set[uuid.UUID] | None = None) -> None:
        self.fail_for = fail_for or set()
        self.enqueued: list[uuid.UUID] = []

    def enqueue_execute(self, task_id: uuid.UUID) -> None:
        if task_id in self.fail_for:
            raise OSError("broker unavailable")
        self.enqueued.append(task_id)


class FakePublisher:
    def prepare(self, source: Path) -> Any:
        return SimpleNamespace(path=source, sha256="a" * 64, size_bytes=8)

    def publish(self, prepared: Any, target: Any, *, allow_recovery: bool) -> Any:
        assert allow_recovery is True
        if "conflict" in str(target):
            raise OutputConflictError(
                MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT,
                "digest mismatch",
            )
        return SimpleNamespace(sha256=prepared.sha256)


def task(base: Path, name: str, digest: str = "a" * 64) -> Any:
    staging = base / "staging" / name / "output" / "result.md"
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.write_bytes(b"prepared")
    return SimpleNamespace(
        id=uuid.uuid4(),
        lease_token=f"lease-{name}",
        target_path=str(base / "output" / f"{name}.md"),
        prepared_output_sha256=digest,
        staging_path=str(staging),
    )


def test_recovery_batches_are_isolated_per_item_and_category(tmp_path: Path) -> None:
    queued_ok, queued_bad = task(tmp_path, "queued-ok"), task(tmp_path, "queued-bad")
    running = task(tmp_path, "running")
    prepared_ok = task(tmp_path, "prepared-ok")
    prepared_conflict = task(tmp_path, "prepared-conflict")
    repository = FakeRecoveryRepository(
        [queued_ok, queued_bad], [running], [prepared_ok, prepared_conflict]
    )
    dispatcher = FakeDispatcher({queued_bad.id})
    recovery = MarkdownCleaningRecovery(
        repository=repository,
        dispatcher=dispatcher,
        publisher=FakePublisher(),
        queue_recovery_interval_seconds=30,
        batch_size=10,
        clock=lambda: NOW,
    )

    result = recovery.recover_batch()

    assert queued_ok.id in dispatcher.enqueued
    assert running.id in dispatcher.enqueued
    assert (prepared_ok.id, "succeeded", "a" * 64) in repository.recovered
    assert (prepared_conflict.id, "output_conflict", None) in repository.recovered
    assert result.queued_errors == 1
    assert result.running_errors == 0
    assert result.prepared_errors == 0


def test_recovery_retries_publication_system_error_without_marking_conflict(
    tmp_path: Path,
) -> None:
    prepared = task(tmp_path, "prepared-system")
    repository = FakeRecoveryRepository([], [], [prepared])

    class SystemFailurePublisher(FakePublisher):
        def publish(self, prepared: Any, target: Any, *, allow_recovery: bool) -> Any:
            raise PublicationSystemError(
                MarkdownCleaningErrorCode.INTERNAL_ERROR, "filesystem unavailable"
            )

    result = MarkdownCleaningRecovery(
        repository=repository,
        dispatcher=FakeDispatcher(),
        publisher=SystemFailurePublisher(),
        queue_recovery_interval_seconds=30,
        batch_size=10,
        clock=lambda: NOW,
    ).recover_batch()

    assert result.prepared_errors == 1
    assert repository.recovered == []


def test_recovery_marks_invalid_prepared_as_invalid_output(tmp_path: Path) -> None:
    prepared = task(tmp_path, "prepared-invalid")
    repository = FakeRecoveryRepository([], [], [prepared])

    class InvalidPublisher(FakePublisher):
        def publish(self, prepared: Any, target: Any, *, allow_recovery: bool) -> Any:
            raise InvalidPreparedOutputError(
                MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT, "bad prepared"
            )

    result = MarkdownCleaningRecovery(
        repository=repository,
        dispatcher=FakeDispatcher(),
        publisher=InvalidPublisher(),
        queue_recovery_interval_seconds=30,
        batch_size=10,
        clock=lambda: NOW,
    ).recover_batch()

    assert result.prepared_errors == 0
    assert (prepared.id, "invalid_output", None) in repository.recovered


def test_repository_separates_expired_running_from_prepared_recovery(
    session: Session,
) -> None:
    plain = _stored_task("plain", lease_token="plain-lease")
    prepared = _stored_task(
        "prepared",
        lease_token="prepared-lease",
        staging_path="/safe/staging/result.md",
        prepared_output_sha256="a" * 64,
    )
    session.add_all((plain, prepared))
    session.commit()
    repository = MarkdownCleaningTaskRepository(session)

    assert [
        item.id for item in repository.list_recoverable_running(now=NOW, limit=10)
    ] == [plain.id]
    assert [
        item.id for item in repository.list_recoverable_prepared(now=NOW, limit=10)
    ] == [prepared.id]
    assert repository.recover_expired_running(
        plain.id, expected_lease_token="plain-lease", now=NOW
    )
    refreshed = repository.get(plain.id)
    assert refreshed is not None
    assert refreshed.status is MarkdownCleaningTaskStatus.QUEUED
    assert refreshed.lease_token is None


def test_repository_reconciles_prepared_only_while_lease_is_expired(
    session: Session,
) -> None:
    prepared = _stored_task(
        "prepared-success",
        lease_token="prepared-lease",
        staging_path="/safe/staging/result.md",
        prepared_output_sha256="b" * 64,
    )
    session.add(prepared)
    session.commit()
    repository = MarkdownCleaningTaskRepository(session)

    assert repository.reconcile_prepared(
        prepared.id, now=NOW, outcome="succeeded", output_sha256="b" * 64
    )
    refreshed = repository.get(prepared.id)
    assert refreshed is not None
    assert refreshed.status is MarkdownCleaningTaskStatus.SUCCEEDED
    assert refreshed.output_sha256 == "b" * 64


def _stored_task(name: str, **values: Any) -> MarkdownCleaningTask:
    status = values.pop("status", MarkdownCleaningTaskStatus.RUNNING)
    lease_token = values.pop("lease_token", None)
    lease_expires_at = values.pop("lease_expires_at", NOW - timedelta(seconds=1))
    return MarkdownCleaningTask(
        caller_id=uuid.uuid4(),
        session_id="session",
        file_id=name,
        request_fingerprint="c" * 64,
        file_storage_path="/input/source.md",
        selected_input_type="local",
        target_path=f"/output/{name}.md",
        status=status,
        attempt_count=1,
        max_attempts=3,
        lease_token=lease_token,
        lease_expires_at=lease_expires_at,
        **values,
    )


def test_first_claim_persists_deadline_and_reclaim_does_not_extend_it(
    session: Session,
) -> None:
    queued = _stored_task(
        "deadline",
        status=MarkdownCleaningTaskStatus.QUEUED,
        lease_token=None,
        lease_expires_at=None,
        processing_deadline=None,
    )
    session.add(queued)
    session.commit()
    repository = MarkdownCleaningTaskRepository(session)
    first = repository.acquire_queued(
        queued.id, now=NOW, lease_seconds=10, processing_timeout_seconds=60
    )
    assert first is not None
    original_deadline = first.processing_deadline
    assert original_deadline in (
        NOW + timedelta(seconds=60),
        (NOW + timedelta(seconds=60)).replace(tzinfo=None),
    )
    assert first.lease_token is not None
    assert repository.recover_expired_running(
        queued.id,
        expected_lease_token=first.lease_token,
        now=NOW + timedelta(seconds=11),
    )
    second = repository.acquire_queued(
        queued.id,
        now=NOW + timedelta(seconds=12),
        lease_seconds=10,
        processing_timeout_seconds=60,
    )
    assert second is not None
    assert second.processing_deadline == original_deadline
