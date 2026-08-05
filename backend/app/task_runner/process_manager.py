import json
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ChildSpec:
    name: str
    command: tuple[str, ...]


DEFAULT_CHILDREN = (
    ChildSpec(
        "worker",
        (
            "celery",
            "-A",
            "app.core.celery_app:celery_app",
            "worker",
            "--loglevel=INFO",
            "--hostname=celery@%h",
        ),
    ),
    ChildSpec(
        "beat",
        (
            "celery",
            "-A",
            "app.core.celery_app:celery_app",
            "beat",
            "--loglevel=INFO",
            "--pidfile=/var/run/celery/beat.pid",
            "--schedule=/var/lib/celery/beat-schedule",
        ),
    ),
)


def _write_state(path: Path, processes: dict[str, subprocess.Popen[bytes]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({name: process.pid for name, process in processes.items()}),
        encoding="utf-8",
    )
    temporary.replace(path)


def _stop_children(
    processes: dict[str, subprocess.Popen[bytes]],
    *,
    grace_seconds: float,
) -> None:
    for process in processes.values():
        if process.poll() is None:
            process.terminate()
    deadline = time.monotonic() + grace_seconds
    for process in processes.values():
        remaining = max(0.0, deadline - time.monotonic())
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            process.kill()
    for process in processes.values():
        if process.poll() is None:
            process.wait()


def run_supervisor(
    children: tuple[ChildSpec, ...] = DEFAULT_CHILDREN,
    *,
    state_path: Path = Path("/var/run/textprocessor/task-runner.json"),
    stop_event: threading.Event | None = None,
    poll_interval: float = 0.2,
    shutdown_grace_seconds: float = 10.0,
) -> int:
    if {child.name for child in children} != {"worker", "beat"} or len(children) != 2:
        raise ValueError("task runner requires exactly worker and beat")
    requested_stop = stop_event or threading.Event()
    processes: dict[str, subprocess.Popen[bytes]] = {}
    try:
        for child in children:
            processes[child.name] = subprocess.Popen(child.command)
        _write_state(state_path, processes)
    except (OSError, ValueError):
        _stop_children(processes, grace_seconds=shutdown_grace_seconds)
        state_path.unlink(missing_ok=True)
        return 1

    try:
        while not requested_stop.wait(poll_interval):
            if any(process.poll() is not None for process in processes.values()):
                return 1
        return 0
    finally:
        _stop_children(processes, grace_seconds=shutdown_grace_seconds)


def main() -> int:
    stop_event = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    return run_supervisor(stop_event=stop_event)


if __name__ == "__main__":
    raise SystemExit(main())
