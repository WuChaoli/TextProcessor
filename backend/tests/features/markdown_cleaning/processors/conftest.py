"""Markdown cleaning processor contract test fixtures."""

from collections.abc import Generator

import pytest


@pytest.fixture(scope="session", autouse=True)
def db() -> Generator[None]:
    yield None
