from collections.abc import Generator

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.features.markdown_cleaning.task_models import (
    MarkdownCleaningTask,  # noqa: F401
)
from app.models import User  # noqa: F401


@pytest.fixture(scope="session", autouse=True)
def db() -> Generator:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
