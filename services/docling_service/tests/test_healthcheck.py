from __future__ import annotations

from pathlib import Path

import pytest

from services.docling_service.healthcheck import check_health


def write_state(path: Path) -> None:
    path.write_text('{"api": 101, "worker": 102}', encoding="utf-8")


def test_health_requires_api_and_worker_processes(tmp_path: Path) -> None:
    state = tmp_path / "processes.json"
    write_state(state)

    assert (
        check_health(
            state,
            "secret",
            "redis://redis:6379/1",
            process_probe=lambda pid: pid == 101,
            http_probe=lambda _key: True,
            redis_probe=lambda _url: True,
        )
        is False
    )


@pytest.mark.parametrize(
    ("http_healthy", "redis_healthy"),
    [(False, True), (True, False)],
)
def test_health_requires_http_and_redis(
    tmp_path: Path, http_healthy: bool, redis_healthy: bool
) -> None:
    state = tmp_path / "processes.json"
    write_state(state)

    assert (
        check_health(
            state,
            "secret",
            "redis://redis:6379/1",
            process_probe=lambda _pid: True,
            http_probe=lambda _key: http_healthy,
            redis_probe=lambda _url: redis_healthy,
        )
        is False
    )


def test_health_passes_only_when_all_probes_pass(tmp_path: Path) -> None:
    state = tmp_path / "processes.json"
    write_state(state)

    assert (
        check_health(
            state,
            "secret",
            "redis://redis:6379/1",
            process_probe=lambda _pid: True,
            http_probe=lambda _key: True,
            redis_probe=lambda url: url.endswith("/1"),
        )
        is True
    )


@pytest.mark.parametrize(
    "content",
    [
        "not-json",
        "{}",
        '{"api": 101}',
        '{"api": 101, "worker": 0}',
        '{"api": 101, "worker": "102"}',
        '{"api": 101, "worker": 102, "extra": 103}',
    ],
)
def test_health_rejects_invalid_state(tmp_path: Path, content: str) -> None:
    state = tmp_path / "processes.json"
    state.write_text(content, encoding="utf-8")

    assert (
        check_health(
            state,
            "secret",
            "redis://redis:6379/1",
            process_probe=lambda _pid: True,
            http_probe=lambda _key: True,
            redis_probe=lambda _url: True,
        )
        is False
    )


def test_health_rejects_redis_database_other_than_one(tmp_path: Path) -> None:
    state = tmp_path / "processes.json"
    write_state(state)

    assert (
        check_health(
            state,
            "secret",
            "redis://redis:6379/0",
            process_probe=lambda _pid: True,
            http_probe=lambda _key: True,
            redis_probe=lambda _url: True,
        )
        is False
    )

