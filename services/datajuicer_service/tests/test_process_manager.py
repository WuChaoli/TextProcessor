import json
import sys
from pathlib import Path

from datajuicer_service.process_manager import ChildSpec, run_supervisor


def command(code: str) -> tuple[str, ...]:
    return (sys.executable, "-c", code)


def test_api_failure_terminates_worker_and_beat(tmp_path: Path) -> None:
    state = tmp_path / "state.json"

    result = run_supervisor(
        (
            ChildSpec("api", command("raise SystemExit(4)")),
            ChildSpec("worker", command("import time; time.sleep(30)")),
            ChildSpec("beat", command("import time; time.sleep(30)")),
        ),
        state_path=state,
        poll_interval=0.01,
        shutdown_grace_seconds=0.2,
        migration_command=None,
    )

    payload = json.loads(state.read_text(encoding="utf-8"))
    assert result != 0
    assert set(payload) == {"api", "worker", "beat"}
    assert all(pid > 0 for pid in payload.values())


def test_migration_failure_prevents_children_from_starting(tmp_path: Path) -> None:
    state = tmp_path / "state.json"

    result = run_supervisor(
        (
            ChildSpec("api", command("import time; time.sleep(30)")),
            ChildSpec("worker", command("import time; time.sleep(30)")),
            ChildSpec("beat", command("import time; time.sleep(30)")),
        ),
        state_path=state,
        migration_command=command("raise SystemExit(2)"),
    )

    assert result != 0
    assert not state.exists()
