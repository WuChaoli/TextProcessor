from pathlib import Path
from unittest.mock import Mock

import pytest

from datajuicer_service.migration_lock import run_migrations


def test_migration_releases_advisory_lock_after_upgrade(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    connection = Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)
    engine = Mock()
    engine.connect.return_value = connection
    create_engine = Mock(return_value=engine)
    monkeypatch.setattr(
        "datajuicer_service.migration_lock.create_engine",
        create_engine,
    )
    upgrade = Mock()
    ini = tmp_path / "alembic.ini"

    run_migrations("postgresql://test", ini, upgrade=upgrade)

    assert connection.execute.call_count == 2
    assert "pg_advisory_lock" in str(
        connection.execute.call_args_list[0].args[0]
    )
    assert "pg_advisory_unlock" in str(
        connection.execute.call_args_list[1].args[0]
    )
    upgrade.assert_called_once()
    assert upgrade.call_args.args[0].get_main_option("script_location") == str(
        tmp_path / "migrations"
    )
    engine.dispose.assert_called_once_with()


def test_migration_releases_lock_when_upgrade_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    connection = Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)
    engine = Mock()
    engine.connect.return_value = connection
    monkeypatch.setattr(
        "datajuicer_service.migration_lock.create_engine",
        Mock(return_value=engine),
    )
    upgrade = Mock(side_effect=RuntimeError("migration failed"))

    with pytest.raises(RuntimeError):
        run_migrations(
            "postgresql://test",
            tmp_path / "alembic.ini",
            upgrade=upgrade,
        )

    assert "pg_advisory_unlock" in str(
        connection.execute.call_args_list[-1].args[0]
    )
    engine.dispose.assert_called_once_with()
