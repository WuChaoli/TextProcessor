from __future__ import annotations

import json
import logging
import os
import urllib.request
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

ProcessProbe = Callable[[int], bool]
HttpProbe = Callable[[str], bool]
RedisProbe = Callable[[str], bool]


def process_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def api_is_healthy(api_key: str) -> bool:
    request = urllib.request.Request(
        "http://localhost:5001/health",
        headers={"X-API-Key": api_key},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status == 200


def redis_is_healthy(redis_url: str) -> bool:
    import redis

    client = redis.Redis.from_url(
        redis_url,
        socket_connect_timeout=5,
        socket_timeout=5,
    )
    try:
        return bool(client.ping())
    finally:
        client.close()


def _load_process_state(state_path: Path) -> dict[str, int]:
    raw_state = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(raw_state, dict) or set(raw_state) != {"api", "worker"}:
        raise ValueError("process state must contain exactly api and worker")
    if any(type(pid) is not int or pid <= 0 for pid in raw_state.values()):
        raise ValueError("process IDs must be positive integers")
    return raw_state


def _uses_redis_database_one(redis_url: str) -> bool:
    parsed = urlparse(redis_url)
    return parsed.scheme in {"redis", "rediss"} and parsed.path == "/1"


def check_health(
    state_path: Path,
    api_key: str,
    redis_url: str,
    process_probe: ProcessProbe = process_is_alive,
    http_probe: HttpProbe = api_is_healthy,
    redis_probe: RedisProbe = redis_is_healthy,
) -> bool:
    """Return true only when process, HTTP, and Redis probes all pass."""
    try:
        if not api_key or not _uses_redis_database_one(redis_url):
            return False
        state = _load_process_state(state_path)
        return (
            all(process_probe(pid) for pid in state.values())
            and http_probe(api_key)
            and redis_probe(redis_url)
        )
    except Exception:
        logger.warning("Docling combined service health check failed", exc_info=True)
        return False


def check_health_from_environment() -> bool:
    state_path = Path(
        os.environ.get(
            "DOCLING_PROCESS_STATE_PATH",
            "/run/textprocessor-docling/processes.json",
        )
    )
    return check_health(
        state_path,
        os.environ.get("DOCLING_SERVE_API_KEY", ""),
        os.environ.get("DOCLING_SERVE_ENG_RQ_REDIS_URL", ""),
    )


if __name__ == "__main__":
    raise SystemExit(0 if check_health_from_environment() else 1)

