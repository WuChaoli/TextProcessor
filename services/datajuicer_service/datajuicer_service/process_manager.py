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
    ChildSpec("api", ("uvicorn", "datajuicer_service.main:create_application", "--factory", "--host=0.0.0.0", "--port=8000")),
    ChildSpec("worker", ("celery", "-A", "datajuicer_service.worker_app:app", "worker", "--loglevel=INFO")),
    ChildSpec(
        "beat",
        (
            "celery",
            "-A",
            "datajuicer_service.worker_app:app",
            "beat",
            "--loglevel=INFO",
            "--pidfile=/var/run/datajuicer/beat.pid",
            "--schedule=/var/lib/datajuicer/beat-schedule",
        ),
    ),
)
DEFAULT_MIGRATION = ("python", "scripts/migrate.py")


def _stop(processes: dict[str, subprocess.Popen[bytes]], grace: float) -> None:
    for process in processes.values():
        if process.poll() is None:
            process.terminate()
    deadline = time.monotonic() + grace
    for process in processes.values():
        try:
            process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            process.kill()
    for process in processes.values():
        if process.poll() is None:
            process.wait()


def _write_state(path: Path, processes: dict[str, subprocess.Popen[bytes]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({name: process.pid for name, process in processes.items()}),
        encoding="utf-8",
    )
    temporary.replace(path)


def run_supervisor(
    children: tuple[ChildSpec, ...] = DEFAULT_CHILDREN,
    *,
    state_path: Path = Path("/var/run/datajuicer/service.json"),
    migration_command: tuple[str, ...] | None = DEFAULT_MIGRATION,
    stop_event: threading.Event | None = None,
    poll_interval: float = 0.2,
    shutdown_grace_seconds: float = 10.0,
) -> int:
    if {child.name for child in children} != {"api", "worker", "beat"} or len(children) != 3:
        raise ValueError("datajuicer requires exactly api, worker and beat")
    if migration_command is not None:
        try:
            if subprocess.run(migration_command, check=False).returncode != 0:
                return 1
        except OSError:
            return 1
    requested_stop = stop_event or threading.Event()
    processes: dict[str, subprocess.Popen[bytes]] = {}
    try:
        for child in children:
            processes[child.name] = subprocess.Popen(child.command)
        _write_state(state_path, processes)
    except OSError:
        _stop(processes, shutdown_grace_seconds)
        state_path.unlink(missing_ok=True)
        return 1
    try:
        while not requested_stop.wait(poll_interval):
            if any(process.poll() is not None for process in processes.values()):
                return 1
        return 0
    finally:
        _stop(processes, shutdown_grace_seconds)


def main() -> int:
    stop_event = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    return run_supervisor(stop_event=stop_event)


if __name__ == "__main__":
    raise SystemExit(main())
