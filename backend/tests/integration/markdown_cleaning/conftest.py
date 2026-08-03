from __future__ import annotations

import uuid
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine

from app.features.markdown_cleaning.input_resolver import InputResolver
from app.features.markdown_cleaning.input_validator import MarkdownInputValidator
from app.features.markdown_cleaning.orchestration import MarkdownCleaningOrchestrator
from app.features.markdown_cleaning.output_validator import (
    MarkdownCleaningOutputValidator,
)
from app.features.markdown_cleaning.processors.pipeline import MarkdownCleaningPipeline
from app.features.markdown_cleaning.publisher import MarkdownCleaningResultPublisher
from app.features.markdown_cleaning.repository import MarkdownCleaningTaskRepository
from app.features.markdown_cleaning.state_machine import MarkdownCleaningTaskStatus
from app.features.markdown_cleaning.task_models import MarkdownCleaningTask
from app.models import User

POSTGRES_URL = "postgresql+psycopg://postgres:changethis@127.0.0.1:5433/app"
REDIS_URL = "redis://127.0.0.1:6396/15"


@pytest.fixture(scope="session")
def pg_engine():
    engine = create_engine(POSTGRES_URL, pool_pre_ping=True)
    with engine.connect() as connection:
        connection.execute(text("select 1"))
    SQLModel.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def pg_session(pg_engine) -> Generator[Session]:
    with Session(pg_engine) as session:
        yield session


@pytest.fixture
def caller(pg_session: Session) -> User:
    user = User(
        email=f"task6-{uuid.uuid4()}@example.com",
        hashed_password="integration-only",
    )
    pg_session.add(user)
    pg_session.commit()
    pg_session.refresh(user)
    return user


@pytest.fixture
def pipeline_roots(tmp_path: Path) -> dict[str, Path]:
    roots = {name: tmp_path / name for name in ("input", "output", "staging")}
    for root in roots.values():
        root.mkdir()
    return roots


def persist_queued_task(
    session: Session,
    caller: User,
    *,
    source: Path,
    target: Path,
    suffix: str = "",
) -> MarkdownCleaningTask:
    task = MarkdownCleaningTask(
        caller_id=caller.id,
        session_id=f"task6-{uuid.uuid4()}{suffix}",
        file_id=f"sample-{uuid.uuid4()}.md",
        request_fingerprint=uuid.uuid4().hex * 2,
        file_storage_path=str(source),
        selected_input_type="local",
        target_path=str(target),
        status=MarkdownCleaningTaskStatus.QUEUED,
        queued_at=datetime.now(UTC),
    )
    session.add(task)
    session.commit()
    session.refresh(task)
    return task


def build_real_orchestrator(
    session: Session, roots: dict[str, Path]
) -> MarkdownCleaningOrchestrator:
    repository = MarkdownCleaningTaskRepository(session)
    return MarkdownCleaningOrchestrator(
        repository=repository,
        resolver=InputResolver(
            input_roots=(roots["input"],),
            allowed_http_hosts=(),
            allowed_http_cidrs=(),
            max_input_bytes=1024 * 1024,
        ),
        input_validator=MarkdownInputValidator(max_input_bytes=1024 * 1024),
        processor=MarkdownCleaningPipeline(staging_root=roots["staging"]),
        output_validator=MarkdownCleaningOutputValidator(),
        publisher=MarkdownCleaningResultPublisher(
            output_roots=(roots["output"],), max_output_bytes=1024 * 1024
        ),
        staging_root=roots["staging"],
        max_output_bytes=1024 * 1024,
        lease_seconds=120,
        processing_timeout_seconds=30,
        lease_renewer=lambda task_id, token: repository.renew_lease(
            task_id,
            lease_token=token,
            now=datetime.now(UTC),
            lease_seconds=120,
        ),
        heartbeat_interval_seconds=1,
    )
