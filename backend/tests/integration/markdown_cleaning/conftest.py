from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
import redis
from fastapi import FastAPI
from sqlalchemy import text
from sqlmodel import Session, create_engine

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


@pytest.fixture(scope="session", autouse=True)
def db() -> Generator[None]:
    """This directory provisions and owns its database per infrastructure test."""
    yield


@dataclass(frozen=True)
class MarkdownCleaningRuntime:
    app: FastAPI
    engine: object
    redis: redis.Redis
    redis_url: str
    source: Path
    target: Path
    staging_root: Path
    session_id: str
    alembic_head: str
    _worker_env: dict[str, str]
    _backend_root: Path

    @contextmanager
    def worker(self):
        process = subprocess.Popen(
            [
                "uv",
                "run",
                "celery",
                "-A",
                "app.core.celery_app:celery_app",
                "worker",
                "-P",
                "solo",
                "-Q",
                "markdown_cleaning",
                "--loglevel=INFO",
                "--without-gossip",
                "--without-mingle",
                "--without-heartbeat",
            ],
            cwd=self._backend_root,
            env=self._worker_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            yield process
        finally:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    check=False,
                    capture_output=True,
                    text=True,
                )
            else:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            assert process.poll() is not None


def _run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return completed.stdout.strip()


@pytest.fixture
def markdown_cleaning_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    database_name = f"tp_md_{uuid.uuid4().hex}"
    container_name = f"tp-md-redis-{uuid.uuid4().hex}"
    admin_dsn = "postgresql://postgres:changethis@127.0.0.1:5433/postgres"
    backend_root = Path(__file__).parents[3]
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    staging_root = tmp_path / "staging"
    for root in (input_root, output_root, staging_root):
        root.mkdir()
    source = input_root / "中文样本.md"
    target = output_root / "清洗结果.md"
    engine = None
    redis_client = None
    created_database = False
    try:
        with psycopg.connect(admin_dsn, autocommit=True) as connection:
            connection.execute(f'CREATE DATABASE "{database_name}"')
        created_database = True
        _run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                container_name,
                "-p",
                "127.0.0.1::6379",
                "redis:7-alpine",
            ],
            cwd=backend_root,
        )
        port_output = _run(
            ["docker", "port", container_name, "6379/tcp"], cwd=backend_root
        )
        redis_port = int(port_output.rsplit(":", 1)[1])
        redis_url = f"redis://127.0.0.1:{redis_port}/0"
        redis_client = redis.Redis.from_url(redis_url)
        deadline = time.monotonic() + 15
        while True:
            try:
                if redis_client.ping():
                    break
            except redis.ConnectionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)

        env = os.environ.copy()
        env.update(
            {
                "POSTGRES_SERVER": "127.0.0.1",
                "POSTGRES_PORT": "5433",
                "POSTGRES_USER": "postgres",
                "POSTGRES_PASSWORD": "changethis",
                "POSTGRES_DB": database_name,
                "CELERY_BROKER_URL": redis_url,
                "MARKDOWN_CLEANING_INPUT_ROOTS": json.dumps([str(input_root)]),
                "MARKDOWN_CLEANING_OUTPUT_ROOTS": json.dumps([str(output_root)]),
                "MARKDOWN_CLEANING_WORKER": json.dumps(
                    {
                        "staging_root": str(staging_root),
                        "output_roots": [str(output_root)],
                        "processing_soft_timeout_seconds": 30,
                        "processing_hard_timeout_seconds": 60,
                    }
                ),
                "PYTHONUTF8": "1",
            }
        )
        _run(["uv", "run", "alembic", "upgrade", "head"], cwd=backend_root, env=env)
        alembic_head = _run(
            ["uv", "run", "alembic", "heads"], cwd=backend_root, env=env
        ).split()[0]
        database_url = (
            f"postgresql+psycopg://postgres:changethis@127.0.0.1:5433/{database_name}"
        )
        engine = create_engine(database_url, pool_pre_ping=True)
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text("select version_num from alembic_version")
                ).scalar_one()
                == alembic_head
            )

        from app.api import deps
        from app.core.celery_app import celery_app
        from app.core.config import MarkdownCleaningWorkerSettings, settings
        from app.main import app

        settings.POSTGRES_SERVER = "127.0.0.1"
        settings.POSTGRES_PORT = 5433
        settings.POSTGRES_DB = database_name
        settings.CELERY_BROKER_URL = redis_url
        settings.MARKDOWN_CLEANING_INPUT_ROOTS = [input_root]
        settings.MARKDOWN_CLEANING_OUTPUT_ROOTS = [output_root]
        settings.MARKDOWN_CLEANING_WORKER = MarkdownCleaningWorkerSettings(
            staging_root=staging_root,
            output_roots=(output_root,),
            processing_soft_timeout_seconds=30,
            processing_hard_timeout_seconds=60,
        )
        monkeypatch.setattr(deps, "engine", engine)
        celery_app.close()
        celery_app.conf.broker_url = redis_url
        caller = User(
            email=f"task6-{uuid.uuid4()}@example.com",
            hashed_password="integration-only",
        )
        with Session(engine) as session:
            session.add(caller)
            session.commit()
            session.refresh(caller)
        app.dependency_overrides[deps.get_current_user] = lambda: caller
        yield MarkdownCleaningRuntime(
            app=app,
            engine=engine,
            redis=redis_client,
            redis_url=redis_url,
            source=source,
            target=target,
            staging_root=staging_root,
            session_id=f"task6-{uuid.uuid4()}",
            alembic_head=alembic_head,
            _worker_env=env,
            _backend_root=backend_root,
        )
    finally:
        try:
            from app.api import deps
            from app.main import app

            app.dependency_overrides.pop(deps.get_current_user, None)
        except ImportError:
            pass
        if engine is not None:
            engine.dispose()
        try:
            from app.core.celery_app import celery_app

            celery_app.close()
        except ImportError:
            pass
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            cwd=backend_root,
            capture_output=True,
        )
        if created_database:
            with psycopg.connect(admin_dsn, autocommit=True) as connection:
                connection.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
                    (database_name,),
                )
                connection.execute(f'DROP DATABASE IF EXISTS "{database_name}"')


@pytest.fixture
def pg_engine(markdown_cleaning_runtime: MarkdownCleaningRuntime):
    return markdown_cleaning_runtime.engine


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
        root.mkdir(exist_ok=True)
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
