from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from sqlmodel import Session

from app.core.config import MarkdownCleaningWorkerSettings, settings
from app.core.db import engine
from app.features.markdown_cleaning.dispatcher import (
    CeleryMarkdownCleaningTaskDispatcher,
)
from app.features.markdown_cleaning.input_resolver import InputResolver
from app.features.markdown_cleaning.input_validator import MarkdownInputValidator
from app.features.markdown_cleaning.orchestration import (
    MarkdownCleaningOrchestrator,
    MarkdownCleaningRecovery,
)
from app.features.markdown_cleaning.output_validator import (
    MarkdownCleaningOutputValidator,
)
from app.features.markdown_cleaning.processors.pipeline import (
    MarkdownCleaningPipeline,
    MarkdownCleaningPipelineLimits,
)
from app.features.markdown_cleaning.publisher import MarkdownCleaningResultPublisher
from app.features.markdown_cleaning.repository import MarkdownCleaningTaskRepository


@contextmanager
def session_scope() -> Iterator[Session]:
    with Session(engine) as session:
        yield session


def renew_lease(
    task_id: uuid.UUID, lease_token: str, *, configured: MarkdownCleaningWorkerSettings
) -> bool:
    with session_scope() as session:
        return MarkdownCleaningTaskRepository(session).renew_lease(
            task_id,
            lease_token=lease_token,
            now=datetime.now(UTC),
            lease_seconds=configured.queue_lease_seconds,
        )


def build_orchestrator(
    session: Session, *, configured: MarkdownCleaningWorkerSettings | None = None
) -> MarkdownCleaningOrchestrator:
    worker = configured or settings.MARKDOWN_CLEANING_WORKER
    Path(worker.staging_root).mkdir(parents=True, exist_ok=True)
    repository = MarkdownCleaningTaskRepository(session)
    return MarkdownCleaningOrchestrator(
        repository=cast(Any, repository),
        resolver=cast(
            Any,
            InputResolver(
                input_roots=settings.MARKDOWN_CLEANING_INPUT_ROOTS,
                allowed_http_hosts=worker.allowed_http_hosts,
                allowed_http_cidrs=worker.allowed_http_cidrs,
                max_input_bytes=worker.max_input_bytes,
                copy_chunk_bytes=worker.copy_chunk_bytes,
                connect_timeout_seconds=worker.connect_timeout_seconds,
                read_timeout_seconds=worker.read_timeout_seconds,
                max_http_redirects=worker.max_http_redirects,
            ),
        ),
        input_validator=cast(
            Any, MarkdownInputValidator(max_input_bytes=worker.max_input_bytes)
        ),
        processor=MarkdownCleaningPipeline(
            staging_root=worker.staging_root,
            limits=MarkdownCleaningPipelineLimits(
                max_input_bytes=worker.max_input_bytes,
                max_output_bytes=worker.max_output_bytes,
                processing_timeout_seconds=worker.processing_soft_timeout_seconds,
            ),
        ),
        output_validator=cast(Any, MarkdownCleaningOutputValidator()),
        publisher=MarkdownCleaningResultPublisher(
            output_roots=worker.output_roots,
            max_output_bytes=worker.max_output_bytes,
            copy_chunk_bytes=worker.copy_chunk_bytes,
        ),
        staging_root=worker.staging_root,
        max_output_bytes=worker.max_output_bytes,
        lease_seconds=worker.queue_lease_seconds,
        processing_timeout_seconds=worker.processing_soft_timeout_seconds,
        lease_renewer=lambda task_id, token: renew_lease(
            task_id, token, configured=worker
        ),
        heartbeat_interval_seconds=max(1.0, worker.queue_lease_seconds / 3),
    )


def build_recovery(
    session: Session, *, configured: MarkdownCleaningWorkerSettings | None = None
) -> MarkdownCleaningRecovery:
    worker = configured or settings.MARKDOWN_CLEANING_WORKER
    return MarkdownCleaningRecovery(
        repository=cast(Any, MarkdownCleaningTaskRepository(session)),
        dispatcher=CeleryMarkdownCleaningTaskDispatcher(),
        publisher=MarkdownCleaningResultPublisher(
            output_roots=worker.output_roots,
            max_output_bytes=worker.max_output_bytes,
            copy_chunk_bytes=worker.copy_chunk_bytes,
        ),
        queue_recovery_interval_seconds=worker.queue_recovery_interval_seconds,
        batch_size=worker.queue_recovery_batch_size,
    )
