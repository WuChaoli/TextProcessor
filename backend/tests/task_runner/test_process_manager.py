import json
import sys
from pathlib import Path

from app.task_runner.process_manager import ChildSpec, run_supervisor


def command(code: str) -> tuple[str, ...]:
    return (sys.executable, "-c", code)


def test_child_failure_terminates_sibling_and_returns_nonzero(tmp_path: Path) -> None:
    state = tmp_path / "task-runner.json"

    result = run_supervisor(
        (
            ChildSpec("worker", command("raise SystemExit(7)")),
            ChildSpec("beat", command("import time; time.sleep(30)")),
        ),
        state_path=state,
        poll_interval=0.01,
        shutdown_grace_seconds=0.2,
    )

    assert result != 0
    assert set(json.loads(state.read_text(encoding="utf-8"))) == {"worker", "beat"}


def test_state_file_has_exact_positive_pids_and_is_atomically_replaced(tmp_path: Path) -> None:
    state = tmp_path / "task-runner.json"
    state.write_text('{"stale": 1}', encoding="utf-8")

    result = run_supervisor(
        (
            ChildSpec("worker", command("raise SystemExit(0)")),
            ChildSpec("beat", command("import time; time.sleep(30)")),
        ),
        state_path=state,
        poll_interval=0.01,
        shutdown_grace_seconds=0.2,
    )

    payload = json.loads(state.read_text(encoding="utf-8"))
    assert result != 0
    assert set(payload) == {"worker", "beat"}
    assert all(isinstance(pid, int) and pid > 0 for pid in payload.values())
    assert not state.with_suffix(".tmp").exists()


def test_clean_startup_failure_terminates_started_child(tmp_path: Path) -> None:
    result = run_supervisor(
        (
            ChildSpec("worker", command("import time; time.sleep(30)")),
            ChildSpec("beat", (str(tmp_path / "missing-command"),)),
        ),
        state_path=tmp_path / "state.json",
        shutdown_grace_seconds=0.2,
    )

    assert result != 0
    assert not (tmp_path / "state.json").exists()
