import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, delete
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from datajuicer_service.jobs.models import Base, DataJuicerJob


@pytest.fixture(scope="session")
def database_url() -> str:
    value = os.environ.get("DATAJUICER_TEST_DATABASE_URL")
    if value is None:
        pytest.skip("DATAJUICER_TEST_DATABASE_URL is required")
    database_name = value.partition("?")[0].rsplit("/", maxsplit=1)[-1]
    if not database_name.endswith("_test"):
        pytest.fail("repository tests require a database ending in _test")
    return value


@pytest.fixture(scope="session")
def engine(database_url: str) -> Iterator[Engine]:
    repository_root = Path(__file__).resolve().parents[4]
    alembic_config = Config(
        repository_root / "services" / "datajuicer_service" / "alembic.ini"
    )
    previous_database_url = os.environ.get("DATAJUICER_DATABASE_URL")
    os.environ["DATAJUICER_DATABASE_URL"] = database_url
    command.downgrade(alembic_config, "base")
    command.upgrade(alembic_config, "head")
    test_engine = create_engine(database_url)
    try:
        yield test_engine
    finally:
        test_engine.dispose()
        command.downgrade(alembic_config, "base")
        if previous_database_url is None:
            del os.environ["DATAJUICER_DATABASE_URL"]
        else:
            os.environ["DATAJUICER_DATABASE_URL"] = previous_database_url


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def clean_job_table(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(delete(DataJuicerJob))


@pytest.fixture
def orchestration_session_factory() -> Iterator[sessionmaker[Session]]:
    sqlite_engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(sqlite_engine)
    yield sessionmaker(sqlite_engine, expire_on_commit=False)
    sqlite_engine.dispose()
