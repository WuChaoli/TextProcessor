import json
import os
from pathlib import Path
from typing import Protocol

from redis import Redis

from app.core.config import settings


class RedisClient(Protocol):
    def ping(self) -> bool: ...


def pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def check_health(
    *,
    state_path: Path = Path("/var/run/textprocessor/task-runner.json"),
    schedule_path: Path = Path("/var/lib/celery/beat-schedule"),
    redis_client: RedisClient | None = None,
) -> bool:
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or set(payload) != {"worker", "beat"}:
            return False
        pids = tuple(payload.values())
        if any(not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 for pid in pids):
            return False
        if not all(pid_is_alive(pid) for pid in pids):
            return False
        if not schedule_path.is_file():
            return False
        client = redis_client or Redis.from_url(settings.CELERY_BROKER_URL)
        return bool(client.ping())
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


def main() -> int:
    return 0 if check_health() else 1


if __name__ == "__main__":
    raise SystemExit(main())
