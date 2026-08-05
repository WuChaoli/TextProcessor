from collections.abc import Callable
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

_MIGRATION_LOCK_KEY = 4_923_933_596_173_428_817


def run_migrations(
    database_url: str,
    alembic_ini: Path,
    *,
    upgrade: Callable[[Config, str], None] = command.upgrade,
) -> None:
    engine = create_engine(database_url)
    config = Config(str(alembic_ini))
    config.set_main_option("script_location", str(alembic_ini.parent / "migrations"))
    try:
        with engine.connect() as connection:
            connection.execute(
                text("SELECT pg_advisory_lock(:key)"),
                {"key": _MIGRATION_LOCK_KEY},
            )
            try:
                upgrade(config, "head")
            finally:
                connection.execute(
                    text("SELECT pg_advisory_unlock(:key)"),
                    {"key": _MIGRATION_LOCK_KEY},
                )
    finally:
        engine.dispose()
