import json
from pathlib import Path

import pytest

from app.task_runner.healthcheck import check_health


class RedisOk:
    def ping(self) -> bool:
        return True


@pytest.mark.parametrize(
    "payload",
    [{}, {"worker": 1}, {"worker": 1, "beat": 2, "extra": 3}, {"worker": 0, "beat": 2}],
)
def test_health_rejects_missing_extra_or_invalid_state(tmp_path: Path, payload: dict[str, int]) -> None:
    state = tmp_path / "state.json"
    state.write_text(json.dumps(payload), encoding="utf-8")
    schedule = tmp_path / "beat-schedule"
    schedule.write_text("ready", encoding="utf-8")

    assert not check_health(state_path=state, schedule_path=schedule, redis_client=RedisOk())


def test_health_requires_live_children_redis_and_schedule(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "state.json"
    state.write_text('{"worker": 11, "beat": 12}', encoding="utf-8")
    schedule = tmp_path / "beat-schedule"
    schedule.write_text("ready", encoding="utf-8")
    monkeypatch.setattr("app.task_runner.healthcheck.pid_is_alive", lambda _pid: True)

    assert check_health(state_path=state, schedule_path=schedule, redis_client=RedisOk())
