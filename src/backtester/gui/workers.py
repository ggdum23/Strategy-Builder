"""Background work so the Qt thread never runs an engine."""

from __future__ import annotations

import json
import multiprocessing
from pathlib import Path

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from .jobs import JobFailure, process_entry


class FunctionThread(QThread):
    """One call off the UI thread. Used for inspect, registry, and pack builds."""

    succeeded = Signal(object)
    failed = Signal(object)

    def __init__(self, fn):
        super().__init__()
        self._fn = fn

    def run(self) -> None:
        try:
            result = self._fn()
        except BaseException as exc:
            # The thread records the failure and returns. A kill of the run
            # process is the supervisor's cancel path, not this handler.
            self.failed.emit(JobFailure.capture(exc))
        else:
            self.succeeded.emit(result)


def _stop_process(proc, status_path) -> None:
    """Stop the worker and, when it detached, the engine children it spawned."""
    import os
    import signal
    import sys

    session = status_path is not None and Path(str(status_path) + ".session").is_file()
    if session and sys.platform != "win32":
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            proc.terminate()
        proc.join(3)
        return
    if sys.platform == "win32":
        import subprocess

        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            check=False,
            capture_output=True,
        )
        proc.join(3)
        return
    proc.terminate()
    proc.join(3)


def read_status(path) -> dict | None:
    file = Path(path)
    if not file.is_file():
        return None
    try:
        return json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


class ProcessSupervisor(QObject):
    """A spawned operator process. Cancel terminates it."""

    progress = Signal(dict)
    finished = Signal(dict)
    failed = Signal(object)
    cancelled = Signal()

    def __init__(self):
        super().__init__()
        self._proc = None
        self._status: Path | None = None
        self._last = None
        self._timer = QTimer(self)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self._poll)

    def running(self) -> bool:
        return self._proc is not None and self._proc.is_alive()

    def start(self, mode: str, config: dict, status_path) -> None:
        if self.running():
            raise RuntimeError("a run is already in progress")
        self._status = Path(status_path)
        self._last = None
        if self._status.exists():
            self._status.unlink()
        flag = Path(str(self._status) + ".cancel")
        if flag.exists():
            flag.unlink()
        context = multiprocessing.get_context("spawn")
        self._proc = context.Process(
            target=process_entry,
            args=(mode, config, str(self._status)),
            name="backtester-gui-run",
            daemon=False,
        )
        self._proc.start()
        self._timer.start()

    def cancel(self) -> None:
        if self._proc is None:
            return
        if self._status is not None:
            Path(str(self._status) + ".cancel").write_text("cancel\n", encoding="utf-8")
        proc = self._proc
        if proc.is_alive():
            _stop_process(proc, self._status)
        if proc.is_alive():
            proc.kill()
            proc.join(3)
        self._timer.stop()
        self._proc = None
        self.cancelled.emit()

    def _poll(self) -> None:
        if self._proc is None or self._status is None:
            return
        payload = read_status(self._status)
        if payload is not None and payload != self._last:
            self._last = payload
            self.progress.emit(payload)
        if self._proc.is_alive():
            return
        self._timer.stop()
        code = self._proc.exitcode
        self._proc.join(1)
        self._proc = None
        payload = read_status(self._status) or {}
        state = payload.get("state")
        if state == "done":
            self.finished.emit(payload)
            return
        if state == "error":
            failure = JobFailure.from_dict(payload.get("failure"))
            if failure is None:
                self.failed.emit(f"worker exited {code}")
                return
            self.failed.emit(failure)
            return
        self.failed.emit(f"worker exited {code} without a finished report")
