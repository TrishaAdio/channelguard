"""Run all three ChannelGuard services under one small supervisor.

Usage:
    python3 infra.py

The service list is intentionally fixed: guard.py, quickreply.py, and
``python -m bot``.  Any child exit is unexpected (including exit status zero)
and is restarted with bounded exponential backoff.  Ctrl+C/SIGTERM shuts every
child down as a group before the supervisor exits.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

import ui

ROOT = Path(__file__).resolve().parent
LOCK_PATH = Path(tempfile.gettempdir()) / "channelguard-infra.lock"
RESTART_WINDOW_SECONDS = 60.0
MAX_RESTARTS_PER_WINDOW = 5
MAX_BACKOFF_SECONDS = 15.0
POLL_SECONDS = 0.2
SHUTDOWN_GRACE_SECONDS = 8.0


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    command: tuple[str, ...]
    color: Callable[[str], str]


SERVICES = (
    ServiceSpec("guard", (sys.executable, "guard.py"), ui.green),
    ServiceSpec("quickreply", (sys.executable, "quickreply.py"), ui.magenta),
    ServiceSpec("bot", (sys.executable, "-m", "bot"), ui.cyan),
)


@dataclass
class ServiceState:
    spec: ServiceSpec
    process: subprocess.Popen | None = None
    reader: threading.Thread | None = None
    restarts: deque[float] = field(default_factory=deque)
    next_start: float = 0.0
    failures: int = 0


class InstanceLock:
    """Non-blocking host-wide process lock retained for this object's lifetime."""

    def __init__(self, path: Path = LOCK_PATH):
        self.path = Path(path)
        self.handle = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, "a+", encoding="utf-8")
        try:
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0, os.SEEK_END)
                if self.handle.tell() == 0:
                    self.handle.write("\0")
                    self.handle.flush()
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(
                    self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                )
        except (OSError, BlockingIOError):
            self.handle.close()
            self.handle = None
            return False

        try:
            self.handle.seek(0)
            self.handle.truncate()
            self.handle.write(f"pid={os.getpid()} cwd={ROOT}\n")
            self.handle.flush()
        except OSError:
            pass
        return True

    def close(self) -> None:
        if self.handle is not None:
            self.handle.close()
            self.handle = None


class Supervisor:
    def __init__(
        self,
        services: Sequence[ServiceSpec] = SERVICES,
        *,
        popen: Callable = subprocess.Popen,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.states = [ServiceState(spec) for spec in services]
        self._popen = popen
        self._monotonic = monotonic
        self._sleep = sleep
        self._stopping = False
        self._fatal = ""

    def request_stop(self, *_args) -> None:
        self._stopping = True

    @staticmethod
    def _environment() -> dict[str, str]:
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        return env

    @staticmethod
    def _prefix(spec: ServiceSpec) -> str:
        return spec.color(f"[{spec.name}]")

    def _read_output(self, state: ServiceState, stream) -> None:
        try:
            for line in iter(stream.readline, ""):
                print(
                    f"{self._prefix(state.spec)} {line.rstrip()}",
                    flush=True,
                )
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    def _start(self, state: ServiceState) -> bool:
        try:
            process = self._popen(
                state.spec.command,
                cwd=str(ROOT),
                env=self._environment(),
                stdin=None,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                start_new_session=(os.name != "nt"),
            )
        except OSError as error:
            ui.error(
                f"{state.spec.name} could not start: "
                f"{type(error).__name__}: {error}"
            )
            return False

        state.process = process
        state.reader = threading.Thread(
            target=self._read_output,
            args=(state, process.stdout),
            name=f"{state.spec.name}-output",
            daemon=True,
        )
        state.reader.start()
        ui.success(
            f"Started {state.spec.name} (pid {process.pid}): "
            + " ".join(state.spec.command)
        )
        return True

    def _schedule_restart(self, state: ServiceState, returncode) -> None:
        now = self._monotonic()
        while (
            state.restarts
            and now - state.restarts[0] > RESTART_WINDOW_SECONDS
        ):
            state.restarts.popleft()
        state.restarts.append(now)
        state.failures += 1
        if len(state.restarts) > MAX_RESTARTS_PER_WINDOW:
            self._fatal = (
                f"{state.spec.name} exited too often "
                f"({len(state.restarts)} times in "
                f"{int(RESTART_WINDOW_SECONDS)}s; last status {returncode})."
            )
            self._stopping = True
            return
        delay = min(2 ** (len(state.restarts) - 1), MAX_BACKOFF_SECONDS)
        state.next_start = now + delay
        ui.warn(
            f"{state.spec.name} exited unexpectedly with status {returncode}; "
            f"restart {len(state.restarts)}/{MAX_RESTARTS_PER_WINDOW} "
            f"in {delay:g}s."
        )

    def _poll(self) -> None:
        now = self._monotonic()
        for state in self.states:
            process = state.process
            if process is None:
                if now >= state.next_start and not self._start(state):
                    self._schedule_restart(state, "start-failed")
                continue
            returncode = process.poll()
            if returncode is None:
                continue
            if state.reader is not None:
                state.reader.join(timeout=0.5)
            state.process = None
            state.reader = None
            self._schedule_restart(state, returncode)

    @staticmethod
    def _signal_process(process, sig: int) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name != "nt":
                os.killpg(process.pid, sig)
            elif sig == signal.SIGTERM:
                process.terminate()
            else:
                process.kill()
        except (OSError, ProcessLookupError):
            pass

    def shutdown(self) -> None:
        running = [
            state for state in self.states
            if state.process is not None and state.process.poll() is None
        ]
        if running:
            ui.info("Stopping ChannelGuard services...")
        for state in running:
            self._signal_process(state.process, signal.SIGTERM)

        deadline = self._monotonic() + SHUTDOWN_GRACE_SECONDS
        while running and self._monotonic() < deadline:
            running = [
                state for state in running
                if state.process is not None and state.process.poll() is None
            ]
            if running:
                self._sleep(0.1)

        for state in running:
            ui.warn(f"Force-stopping {state.spec.name}.")
            self._signal_process(state.process, signal.SIGKILL)

        for state in self.states:
            if state.process is not None:
                try:
                    state.process.wait(timeout=1)
                except (subprocess.TimeoutExpired, OSError):
                    pass
            if state.reader is not None:
                state.reader.join(timeout=1)
            state.process = None
            state.reader = None

    def run(self) -> int:
        for state in self.states:
            self._start(state)
        try:
            while not self._stopping:
                self._poll()
                self._sleep(POLL_SECONDS)
        finally:
            self.shutdown()
        if self._fatal:
            ui.error(self._fatal)
            return 1
        return 0


def main() -> int:
    lock = InstanceLock()
    if not lock.acquire():
        ui.error(
            f"Another infra.py supervisor owns {LOCK_PATH}. "
            "Stop it before launching a second copy."
        )
        return 1

    supervisor = Supervisor()
    previous = {}

    def stop_handler(signum, _frame) -> None:
        ui.warn(f"Received signal {signum}; shutting down.")
        supervisor.request_stop()

    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, stop_handler)
        ui.banner("ChannelGuard infrastructure")
        ui.info("Launching guard.py, quickreply.py, and python -m bot.")
        ui.info(f"Host lock: {LOCK_PATH}")
        print(ui.dim("Ctrl+C to stop all three services."), flush=True)
        return supervisor.run()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
