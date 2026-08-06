import json
import os
from pathlib import Path

import httpx
from redis import Redis

from datajuicer_service.core.config import get_settings


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def check_health(
    state_path: Path = Path("/var/run/datajuicer/service.json"),
    schedule_path: Path = Path("/var/lib/datajuicer/beat-schedule"),
) -> bool:
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or set(state) != {"api", "worker", "beat"}:
            return False
        if any(not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 or not _alive(pid) for pid in state.values()):
            return False
        if not schedule_path.is_file():
            return False
        if httpx.get("http://127.0.0.1:8000/ready", timeout=3).status_code != 200:
            return False
        return bool(Redis.from_url(get_settings().celery_broker_url).ping())
    except (OSError, ValueError, TypeError, json.JSONDecodeError, httpx.HTTPError):
        return False


if __name__ == "__main__":
    raise SystemExit(0 if check_health() else 1)
