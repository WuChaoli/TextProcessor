from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.features.markdown_cleaning.input_validator import (
    MarkdownInputError,
    MarkdownInputErrorCode,
    ValidatedMarkdownInput,
)
from app.features.markdown_cleaning.orchestration import (
    LeaseLostError,
    MarkdownCleaningOrchestrator,
    RetryableWorkerError,
)
from app.features.markdown_cleaning.processors.errors import (
    MarkdownCleaningErrorCode,
    MarkdownCleaningProcessorError,
)
from app.features.markdown_cleaning.processors.models import (
    MarkdownCleaningSummary,
    ProcessorResult,
)
from app.features.markdown_cleaning.publisher import (
    PreparedMarkdownResult,
    PublishedMarkdownResult,
)
from app.features.markdown_cleaning.staging import StagingLayout

NOW = datetime(2026, 8, 3, 8, tzinfo=UTC)


@pytest.fixture(scope="session", autouse=True)
def db() -> None:
    """Override the backend-wide PostgreSQL fixture for this pure unit module."""


@dataclass
class FakeRepository:
    task: Any
    fail_succeeded: bool = False
    reject_progress: bool = False

    def __post_init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def acquire_queued(self, task_id: uuid.UUID, **kwargs: Any) -> Any:
        self.calls.append(("claim", kwargs))
        return self.task

    def update_progress(self, task_id: uuid.UUID, **kwargs: Any) -> bool:
        self.calls.append(("progress", kwargs))
        return not self.reject_progress

    def save_prepared(self, task_id: uuid.UUID, **kwargs: Any) -> bool:
        self.calls.append(("prepare", kwargs))
        return True

    def mark_publishing(self, task_id: uuid.UUID, **kwargs: Any) -> bool:
        self.calls.append(("publishing", kwargs))
        return True

    def mark_succeeded(self, task_id: uuid.UUID, **kwargs: Any) -> bool:
        self.calls.append(("succeeded", kwargs))
        if self.fail_succeeded:
            raise OSError("database unavailable")
        return True

    def mark_failed(self, task_id: uuid.UUID, **kwargs: Any) -> bool:
        self.calls.append(("failed", kwargs))
        return True


class FakeResolver:
    def __init__(self, resolved: Any) -> None:
        self.resolved = resolved
        self.calls = 0

    def resolve(self, task: Any, layout: StagingLayout) -> Any:
        self.calls += 1
        layout.prepare()
        layout.original_source.write_bytes(b"source\n")
        return self.resolved(layout)


class FakeValidator:
    def validate(self, resolved: Any, layout: StagingLayout, **kwargs: Any) -> Any:
        layout.processor_source.write_bytes(b"source\n")
        digest = hashlib.sha256(b"source\n").hexdigest()
        return ValidatedMarkdownInput(
            original_path=layout.original_source,
            original_size_bytes=7,
            original_sha256=digest,
            processor_path=layout.processor_source,
            processor_size_bytes=7,
            processor_sha256=digest,
        )


class FakeProcessor:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[Path, Path, datetime | None]] = []

    def process(
        self,
        source_path: Path,
        destination_path: Path,
        *,
        deadline: datetime | None = None,
    ) -> ProcessorResult:
        self.calls.append((source_path, destination_path, deadline))
        if self.error is not None:
            raise self.error
        destination_path.write_bytes(b"clean\n")
        return ProcessorResult(
            output_path=destination_path,
            input_sha256=hashlib.sha256(b"source\n").hexdigest(),
            output_sha256=hashlib.sha256(b"clean\n").hexdigest(),
            contract_version="markdown_cleaning_v1",
            summary=MarkdownCleaningSummary(1, 2, 3, 4, 5, 6, 7),
            input_bytes=7,
            output_bytes=6,
        )


class FakeOutputValidator:
    def validate(self, result: ProcessorResult, **kwargs: Any) -> ProcessorResult:
        return result


class FakePublisher:
    def __init__(self) -> None:
        self.published: list[tuple[Path, bool]] = []
        self.error: Exception | None = None

    def prepare(self, source: Path) -> PreparedMarkdownResult:
        data = source.read_bytes()
        return PreparedMarkdownResult(
            source, hashlib.sha256(data).hexdigest(), len(data)
        )

    def publish(
        self, prepared: PreparedMarkdownResult, target: Path, *, allow_recovery: bool
    ) -> PublishedMarkdownResult:
        if self.error is not None:
            raise self.error
        self.published.append((target, allow_recovery))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(prepared.path.read_bytes())
        return PublishedMarkdownResult(
            target, prepared.sha256, prepared.size_bytes, False
        )


def build_orchestrator(
    tmp_path: Path,
    *,
    deadline: datetime = NOW + timedelta(minutes=1),
    processor_error: Exception | None = None,
    fail_succeeded: bool = False,
    clock: Any = lambda: NOW,
) -> tuple[
    MarkdownCleaningOrchestrator,
    FakeRepository,
    FakeResolver,
    FakeProcessor,
    FakePublisher,
    Any,
]:
    task_id = uuid.uuid4()
    task = SimpleNamespace(
        id=task_id,
        lease_token="lease-new",
        processing_deadline=deadline,
        target_path=str(tmp_path / "output" / "result.md"),
        attempt_count=1,
        max_attempts=3,
        input_sha256=None,
    )
    repository = FakeRepository(task, fail_succeeded=fail_succeeded)
    resolver = FakeResolver(
        lambda layout: SimpleNamespace(
            path=layout.original_source,
            size_bytes=7,
            sha256=hashlib.sha256(b"source\n").hexdigest(),
            source_suffix=".md",
        )
    )
    processor = FakeProcessor(processor_error)
    publisher = FakePublisher()
    orchestrator = MarkdownCleaningOrchestrator(
        repository=repository,
        resolver=resolver,
        input_validator=FakeValidator(),
        processor=processor,
        output_validator=FakeOutputValidator(),
        publisher=publisher,
        staging_root=tmp_path / "staging",
        max_output_bytes=1024,
        lease_seconds=120,
        clock=clock,
    )
    return orchestrator, repository, resolver, processor, publisher, task


def test_execute_runs_ordered_pipeline_with_deadline_and_safe_cleanup(
    tmp_path: Path,
) -> None:
    orchestrator, repository, _, processor, publisher, task = build_orchestrator(
        tmp_path
    )

    orchestrator.execute(task.id)

    assert [name for name, _ in repository.calls] == [
        "claim",
        "progress",
        "progress",
        "prepare",
        "publishing",
        "succeeded",
    ]
    assert processor.calls == [
        (
            tmp_path / "staging" / str(task.id) / "input" / "source.md",
            tmp_path / "staging" / str(task.id) / "output" / "result.md",
            task.processing_deadline,
        )
    ]
    assert publisher.published == [(Path(task.target_path), False)]
    assert Path(task.target_path).read_bytes() == b"clean\n"
    assert not (tmp_path / "staging" / str(task.id)).exists()
    tokens = {
        values["lease_token"] for name, values in repository.calls if name != "claim"
    }
    assert tokens == {"lease-new"}


def test_expired_task_does_not_touch_source_or_destination(tmp_path: Path) -> None:
    orchestrator, repository, resolver, processor, publisher, task = build_orchestrator(
        tmp_path, deadline=NOW
    )

    orchestrator.execute(task.id)

    assert resolver.calls == 0
    assert processor.calls == []
    assert publisher.published == []
    assert repository.calls[-1][0] == "failed"
    assert repository.calls[-1][1]["error_code"] == "PROCESSING_TIMEOUT"


def test_deadline_expiring_during_processing_prevents_publish(tmp_path: Path) -> None:
    ticks = iter(
        (NOW, NOW, NOW, NOW, NOW + timedelta(minutes=2), NOW + timedelta(minutes=2))
    )
    orchestrator, repository, _, processor, publisher, task = build_orchestrator(
        tmp_path, clock=lambda: next(ticks)
    )

    orchestrator.execute(task.id)

    assert len(processor.calls) == 1
    assert publisher.published == []
    assert repository.calls[-1][0] == "failed"
    assert repository.calls[-1][1]["error_code"] == "PROCESSING_TIMEOUT"


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (
            MarkdownInputError(MarkdownInputErrorCode.INVALID_UTF8, "invalid"),
            "INVALID_UTF8",
        ),
        (
            MarkdownCleaningProcessorError(
                MarkdownCleaningErrorCode.MARKDOWN_PARSE_FAILED, "bad"
            ),
            "MARKDOWN_PARSE_FAILED",
        ),
        (
            MarkdownCleaningProcessorError(
                MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT, "bad"
            ),
            "INVALID_PROCESSOR_OUTPUT",
        ),
    ],
)
def test_deterministic_errors_are_terminal(
    tmp_path: Path, error: Exception, code: str
) -> None:
    orchestrator, repository, _, _, _, task = build_orchestrator(
        tmp_path, processor_error=error
    )

    orchestrator.execute(task.id)

    assert repository.calls[-1][0] == "failed"
    assert repository.calls[-1][1]["error_code"] == code


def test_publish_conflict_is_terminal_output_conflict(tmp_path: Path) -> None:
    orchestrator, repository, _, _, publisher, task = build_orchestrator(tmp_path)
    publisher.error = MarkdownCleaningProcessorError(
        MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT, "exists"
    )

    orchestrator.execute(task.id)

    assert repository.calls[-1][0] == "failed"
    assert repository.calls[-1][1]["error_code"] == "OUTPUT_CONFLICT"


def test_transient_error_is_retryable_only_below_attempt_limit(tmp_path: Path) -> None:
    orchestrator, repository, _, _, _, task = build_orchestrator(
        tmp_path, processor_error=OSError("temporary storage failure")
    )
    with pytest.raises(RetryableWorkerError):
        orchestrator.execute(task.id)
    assert all(name != "failed" for name, _ in repository.calls)

    task.attempt_count = task.max_attempts
    orchestrator.execute(task.id)
    assert repository.calls[-1][0] == "failed"
    assert repository.calls[-1][1]["error_code"] == "INTERNAL_ERROR"


def test_old_worker_cannot_continue_after_conditional_write_rejected(
    tmp_path: Path,
) -> None:
    orchestrator, repository, _, processor, publisher, task = build_orchestrator(
        tmp_path
    )
    repository.reject_progress = True

    with pytest.raises(LeaseLostError):
        orchestrator.execute(task.id)

    assert processor.calls == []
    assert publisher.published == []


def test_database_failure_after_publish_is_left_for_hash_recovery(
    tmp_path: Path,
) -> None:
    orchestrator, repository, _, _, publisher, task = build_orchestrator(
        tmp_path, fail_succeeded=True
    )

    with pytest.raises(RetryableWorkerError):
        orchestrator.execute(task.id)

    assert publisher.published == [(Path(task.target_path), False)]
    assert (tmp_path / "staging" / str(task.id)).exists()
