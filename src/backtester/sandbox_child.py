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
from pathlib import Path


def _send(payload: dict) -> None:
    line = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8")
    os.write(1, line + b"\n")


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


def _apply_limits() -> None:
    path = Path("limits.json")
    if not path.is_file():
        return
    spec = json.loads(path.read_text(encoding="utf-8"))
    path.unlink()
    if os.name == "nt":
        return
    import resource

    memory = int(spec.get("memory_bytes") or 0)
    cpu = int(spec.get("cpu_seconds") or 0)
    if memory > 0:
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
        resource.setrlimit(resource.RLIMIT_DATA, (memory, memory))
    if cpu > 0:
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))


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


def main() -> int:
    try:
        _apply_limits()
        _extend_path()
        sys.argv = ["strategy"]
        try:
            strategy, wants, has_day_end = _load()
        except Exception as exc:
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
            if op == "bar":
                try:
                    target = strategy.on_bar(message["bar"], _second(message))
                except Exception as exc:
                    _send({"op": "error", "error": f"{type(exc).__name__}: {exc}"})
                    return 1
                _send({"op": "decision", "target": _encode(target)})
                continue
            if op == "day_end":
                try:
                    hook = getattr(strategy, "on_day_end", None)
                    if callable(hook):
                        hook(message.get("summary") or {})
                except Exception as exc:
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


if __name__ == "__main__":
    sys.exit(main())
