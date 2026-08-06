from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

from services.docling_service.process_manager import ProcessSpec, run_supervisor


def wait_until_exists(path: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.01)
    raise AssertionError(f"state file was not created: {path}")


def process_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def sleeping_process(name: str) -> ProcessSpec:
    return ProcessSpec(
        name,
        (sys.executable, "-c", "import time; time.sleep(60)"),
    )


def test_child_failure_terminates_sibling_and_returns_nonzero(tmp_path: Path) -> None:
    fast_failure = ProcessSpec(
        "api",
        (sys.executable, "-c", "raise SystemExit(23)"),
    )

    exit_code = run_supervisor(
        (fast_failure, sleeping_process("worker")),
        tmp_path / "processes.json",
        grace_seconds=0.2,
    )

    assert exit_code != 0


def test_state_file_contains_both_child_pids(tmp_path: Path) -> None:
    state_path = tmp_path / "processes.json"
    stop_event = threading.Event()
    result: list[int] = []
    thread = threading.Thread(
        target=lambda: result.append(
            run_supervisor(
                (sleeping_process("api"), sleeping_process("worker")),
                state_path,
                0.2,
                stop_event,
            )
        )
    )

    thread.start()
    wait_until_exists(state_path)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert set(state) == {"api", "worker"}
    assert all(isinstance(pid, int) and pid > 0 for pid in state.values())
    stop_event.set()
    thread.join(timeout=5)

    assert result == [0]


def test_shutdown_terminates_children_and_removes_state_file(tmp_path: Path) -> None:
    state_path = tmp_path / "processes.json"
    stop_event = threading.Event()
    thread = threading.Thread(
        target=run_supervisor,
        args=(
            (sleeping_process("api"), sleeping_process("worker")),
            state_path,
            0.2,
            stop_event,
        ),
    )

    thread.start()
    wait_until_exists(state_path)
    child_pids = tuple(json.loads(state_path.read_text(encoding="utf-8")).values())
    stop_event.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert not state_path.exists()
    assert all(not process_is_alive(pid) for pid in child_pids)
