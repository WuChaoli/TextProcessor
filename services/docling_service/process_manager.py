from __future__ import annotations

import json
import logging
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProcessSpec:
    name: str
    argv: tuple[str, ...]


def _publish_state(
    state_path: Path, processes: dict[str, subprocess.Popen[bytes]]
) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = state_path.with_suffix(f"{state_path.suffix}.tmp")
    temporary_path.write_text(
        json.dumps({name: process.pid for name, process in processes.items()}),
        encoding="utf-8",
    )
    temporary_path.replace(state_path)


def _terminate_processes(
    processes: dict[str, subprocess.Popen[bytes]], grace_seconds: float
) -> None:
    for process in processes.values():
        if process.poll() is None:
            process.terminate()

    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if all(process.poll() is not None for process in processes.values()):
            return
        time.sleep(0.05)

    for process in processes.values():
        if process.poll() is None:
            process.kill()
    for process in processes.values():
        process.wait()


def run_supervisor(
    specs: tuple[ProcessSpec, ...],
    state_path: Path,
    grace_seconds: float,
    stop_event: threading.Event | None = None,
) -> int:
    """Run all children; if one exits, terminate the rest and return non-zero."""
    if not specs:
        raise ValueError("at least one child process is required")
    if len({spec.name for spec in specs}) != len(specs):
        raise ValueError("child process names must be unique")
    if grace_seconds < 0:
        raise ValueError("grace_seconds must be non-negative")

    shutdown = stop_event or threading.Event()
    processes: dict[str, subprocess.Popen[bytes]] = {}
    previous_handlers: dict[signal.Signals, signal._HANDLER] = {}

    def request_shutdown(_signum: int, _frame: object) -> None:
        shutdown.set()

    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[sig] = signal.getsignal(sig)
            signal.signal(sig, request_shutdown)

    exit_code = 0
    try:
        for spec in specs:
            processes[spec.name] = subprocess.Popen(spec.argv, shell=False)
        _publish_state(state_path, processes)

        while not shutdown.wait(timeout=0.05):
            for name, process in processes.items():
                child_exit_code = process.poll()
                if child_exit_code is None:
                    continue
                logger.error(
                    "Docling child process exited unexpectedly: name=%s exit_code=%d",
                    name,
                    child_exit_code,
                )
                exit_code = child_exit_code if child_exit_code != 0 else 1
                shutdown.set()
                break
    except Exception:
        logger.exception("Docling process supervisor failed")
        exit_code = 1
    finally:
        _terminate_processes(processes, grace_seconds)
        state_path.unlink(missing_ok=True)
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
    return exit_code


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    specs = (
        ProcessSpec(
            "api",
            ("docling-serve", "run", "--host", "0.0.0.0", "--port", "5001"),
        ),
        ProcessSpec("worker", ("docling-serve", "rq-worker")),
    )
    return run_supervisor(
        specs,
        Path("/run/textprocessor-docling/processes.json"),
        grace_seconds=20.0,
    )


if __name__ == "__main__":
    raise SystemExit(main())
