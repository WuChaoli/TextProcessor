import os
from pathlib import Path

from datajuicer_service.migration_lock import run_migrations


def main() -> None:
    database_url = os.environ.get("DATAJUICER_DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATAJUICER_DATABASE_URL is required")
    service_root = Path(__file__).resolve().parents[1]
    run_migrations(database_url, service_root / "alembic.ini")


if __name__ == "__main__":
    main()
