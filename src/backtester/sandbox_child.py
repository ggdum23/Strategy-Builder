"""Strategy process for the one-bar referee.

The parent launches this file with ``python -I`` inside the sandbox. stdin and
stdout are the protocol. One request is read, ``on_bar`` runs, one decision is
written. The next bar is not in the pipe until that decision is written, so a
strategy that reads stdin here blocks until the per-call timeout.

This file is copied into the private directory next to ``strategy.py``. It
uses the standard library only. It does not embed a data path, a scale, or a
ticker.
"""

from __future__ import annotations

import json
import math
import os
import sys
import threading
from pathlib import Path

# Decisions go here. File descriptor 1 stays the protocol unless the parent
# asked for a captured print log. The referee CLI does not, so a second line
# on stdout is still a protocol failure. A designer run does, so print()
# cannot forge a decision and a large print cannot fill the pipe.
_PROTO = 1
_STDIO_LIMIT = 256 * 1024
_stdio_read = None
_stdio_thread = None
_stdio_done = False


def _send(payload: dict) -> None:
    line = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8")
    os.write(_PROTO, line + b"\n")


def _prepare_stdio(capture: bool) -> None:
    """Park the protocol on another fd and drain prints into ``stdio.log``.

    The drain keeps reading after the file is full, which is what stops an
    8 MiB print from blocking ``on_bar``. The file is the strategy's cwd, so
    the strategy can see the cap. Closing the write end on the way out lets
    the drain finish before this process exits.
    """
    global _PROTO, _stdio_read, _stdio_thread
    if not capture:
        return
    proto = os.dup(1)
    try:
        os.set_inheritable(proto, False)
    except OSError:
        pass
    read_fd, write_fd = os.pipe()
    os.dup2(write_fd, 1)
    os.dup2(write_fd, 2)
    os.close(write_fd)
    _PROTO = proto
    _stdio_read = read_fd

    def drain() -> None:
        stored = 0
        try:
            handle = open("stdio.log", "wb")
        except OSError:
            handle = None
        try:
            while True:
                try:
                    chunk = os.read(read_fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                if handle is None:
                    continue
                room = _STDIO_LIMIT - stored
                if room > 0:
                    piece = chunk[:room]
                    handle.write(piece)
                    handle.flush()
                    stored += len(piece)
        finally:
            if handle is not None:
                handle.close()

    _stdio_thread = threading.Thread(target=drain, name="bt-stdio", daemon=True)
    _stdio_thread.start()


def _finish_stdio() -> None:
    """Close the print pipe and wait until ``stdio.log`` has the bytes."""
    global _stdio_done
    if _stdio_done or _stdio_thread is None:
        return
    _stdio_done = True
    for fd in (1, 2):
        try:
            os.close(fd)
        except OSError:
            pass
    _stdio_thread.join(timeout=2)


def _recv() -> dict | None:
    buf = bytearray()
    while True:
        chunk = os.read(0, 1)
        if not chunk:
            if not buf:
                return None
            break
        buf.extend(chunk)
        if chunk == b"\n":
            break
        if len(buf) > 1_000_000:
            raise ValueError("request is too large")
    return json.loads(bytes(buf).decode("utf-8"))


def _apply_limits() -> bool:
    """Apply rlimits when the host asked, and report whether prints are captured.

    The limits file is removed before the strategy is loaded. Windows has no
    rlimit; the job object is the cap there. The capture flag is independent.
    """
    path = Path("limits.json")
    if not path.is_file():
        return False
    spec = json.loads(path.read_text(encoding="utf-8"))
    path.unlink()
    capture = bool(spec.get("capture_stdio"))
    if os.name != "nt":
        import resource

        memory = int(spec.get("memory_bytes") or 0)
        cpu = int(spec.get("cpu_seconds") or 0)
        if memory > 0:
            resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
            resource.setrlimit(resource.RLIMIT_DATA, (memory, memory))
        if cpu > 0:
            resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    return capture


def _extend_path() -> None:
    path = Path("runtime.json")
    if not path.is_file():
        return
    entries = json.loads(path.read_text(encoding="utf-8"))
    path.unlink()
    for entry in entries:
        if entry and entry not in sys.path:
            sys.path.append(entry)


def _load():
    import importlib.util

    file_path = Path("strategy.py")
    spec = importlib.util.spec_from_file_location("strategy", file_path)
    if spec is None or spec.loader is None:
        raise ImportError("strategy.py cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "Strategy"):
        raise AttributeError("strategy.py does not define Strategy")
    cls = module.Strategy
    wants = bool(getattr(cls, "WANTS_EXTENDED_HOURS", False))
    has_day_end = callable(getattr(cls, "on_day_end", None))
    strategy = cls()
    wants = bool(getattr(strategy, "WANTS_EXTENDED_HOURS", wants))
    has_day_end = callable(getattr(strategy, "on_day_end", None)) or has_day_end
    return strategy, wants, has_day_end


def _encode(target):
    if target is None:
        return None
    if isinstance(target, bool):
        return target
    if isinstance(target, int) and not isinstance(target, bool):
        return int(target)
    if isinstance(target, float):
        if math.isnan(target):
            return "NaN"
        if target == math.inf:
            return "Infinity"
        if target == -math.inf:
            return "-Infinity"
        return float(target)
    name = type(target).__name__
    if name in {"bool_", "bool8"}:
        return bool(target)
    try:
        value = float(target)
    except (TypeError, ValueError):
        return {"unsupported": name}
    if math.isnan(value):
        return "NaN"
    if value == math.inf:
        return "Infinity"
    if value == -math.inf:
        return "-Infinity"
    return value


def _second(message: dict):
    if "account" in message:
        return message["account"]
    return message.get("state")


def _json_value(value):
    """A snapshot the parent can store. Callables and open handles stay here."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite")
        return value
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    raise TypeError(type(value).__name__)


def _public_state(strategy) -> dict:
    values = {}
    for key, value in list(getattr(strategy, "__dict__", {}).items()):
        if not isinstance(key, str) or key.startswith("_"):
            continue
        try:
            encoded = _json_value(value)
            json.dumps(encoded, allow_nan=False)
        except (TypeError, ValueError, OverflowError):
            continue
        values[key] = encoded
        if len(json.dumps(values, allow_nan=False)) > 200_000:
            values.pop(key, None)
            break
    return values


def main() -> int:
    try:
        capture = _apply_limits()
        _prepare_stdio(capture)
        _extend_path()
        sys.argv = ["strategy"]
        try:
            strategy, wants, has_day_end = _load()
        except Exception as exc:
            # Wait for hello so the error is the reply, not a write into a
            # pipe the parent has not opened yet.
            _recv()
            _send({"op": "error", "error": f"{type(exc).__name__}: {exc}"})
            return 1
        hello = _recv()
        if hello is None or hello.get("op") != "hello":
            _send({"op": "error", "error": "expected hello"})
            return 2
        _send(
            {
                "op": "ready",
                "wants_extended_hours": wants,
                "has_day_end": has_day_end,
            }
        )
        while True:
            message = _recv()
            if message is None:
                return 0
            op = message.get("op")
            if op == "stop":
                return 0
            if op == "state":
                _send({"op": "state", "values": _public_state(strategy)})
                continue
            if op == "bar":
                try:
                    target = strategy.on_bar(message["bar"], _second(message))
                except (Exception, SystemExit) as exc:
                    _send({"op": "error", "error": f"{type(exc).__name__}: {exc}"})
                    return 1
                _send({"op": "decision", "target": _encode(target)})
                continue
            if op == "day_end":
                try:
                    hook = getattr(strategy, "on_day_end", None)
                    if callable(hook):
                        hook(message.get("summary") or {})
                except (Exception, SystemExit) as exc:
                    _send({"op": "error", "error": f"{type(exc).__name__}: {exc}"})
                    return 1
                _send({"op": "decision", "target": None})
                continue
            _send({"op": "error", "error": f"unknown op {op!r}"})
            return 2
    except BrokenPipeError:
        return 0
    except Exception as exc:
        try:
            _send({"op": "error", "error": f"{type(exc).__name__}: {exc}"})
        except Exception:
            pass
        return 1
    finally:
        _finish_stdio()


if __name__ == "__main__":
    sys.exit(main())
