"""Strategy process for the one-bar referee.

The parent launches this file with ``python -I -S`` inside the sandbox, or
re-invokes a frozen executable with ``--sandbox-child``. stdin and stdout are
the protocol. One request is read, ``on_bar`` runs, one decision is written.
The next bar is not in the pipe until that decision is written, so a strategy
that reads stdin here blocks until the per-call timeout.

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
import traceback
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


_IPC = None


def _ipc():
    """The frame codec. A source jail runs with ``python -I``, which leaves the
    script directory off ``sys.path``, so the sibling file is loaded by path.
    A frozen child imports the package module.
    """
    global _IPC
    if _IPC is not None:
        return _IPC
    try:
        from . import bar_ipc
    except ImportError:
        import importlib.util

        path = Path(__file__).resolve().parent / "bar_ipc.py"
        spec = importlib.util.spec_from_file_location("bar_ipc", path)
        if spec is None or spec.loader is None:
            raise ImportError("bar_ipc.py is missing")
        bar_ipc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bar_ipc)
    _IPC = bar_ipc
    return bar_ipc


def _send(payload: dict) -> None:
    frame = _ipc().dumps(payload)
    view = memoryview(frame)
    while view:
        count = os.write(_PROTO, view)
        view = view[count:]


def _exception_error(exc: BaseException) -> dict:
    """One protocol line for a strategy exception.

    ``error`` is ``Type: message`` so the parent banner can name the
    exception. ``trace`` is the strategy process traceback, which the
    operator log shows and the banner does not. The tail is kept when the
    text is long, because the raised line is at the end.
    """
    trace = traceback.format_exc()
    if len(trace) > 16_000:
        trace = trace[-16_000:]
    return {"op": "error", "error": f"{type(exc).__name__}: {exc}", "trace": trace}


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


_stdin_buf = b""


def _recv() -> dict | None:
    """Read one length-prefixed frame. Bytes past that frame stay buffered."""
    global _stdin_buf
    ipc = _ipc()
    while len(_stdin_buf) < 4:
        chunk = os.read(0, 65536)
        if not chunk:
            if not _stdin_buf:
                return None
            raise ValueError("request is too large")
        _stdin_buf += chunk
        if len(_stdin_buf) > ipc.MAX_FRAME + 4:
            raise ValueError("request is too large")
    length = int.from_bytes(_stdin_buf[:4], "little")
    if length > ipc.MAX_FRAME:
        raise ValueError("request is too large")
    need = 4 + length
    while len(_stdin_buf) < need:
        chunk = os.read(0, 65536)
        if not chunk:
            raise ValueError("request is too large")
        _stdin_buf += chunk
        if len(_stdin_buf) > ipc.MAX_FRAME + 4:
            raise ValueError("request is too large")
    body = _stdin_buf[4:need]
    _stdin_buf = _stdin_buf[need:]
    return ipc.loads(body)


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

        # The parent wrote this ceiling. Bubblewrap's user namespace makes 64
        # the whole jail. Landlock shares the host uid, so the ceiling is the
        # task count from before the ruleset plus the same 64-task headroom.
        # clone3 returns ENOSYS; threads use clone with CLONE_THREAD, and a
        # further process is EPERM from the filter.
        nproc = spec.get("nproc")
        if isinstance(nproc, int) and not isinstance(nproc, bool) and nproc > 0:
            _set_nproc(int(nproc))
        memory = int(spec.get("memory_bytes") or 0)
        cpu = int(spec.get("cpu_seconds") or 0)
        if memory > 0:
            resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
            resource.setrlimit(resource.RLIMIT_DATA, (memory, memory))
        if cpu > 0:
            resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    return capture


def _set_nproc(ceiling: int) -> None:
    """Apply the parent's process ceiling without raising the hard limit."""
    import resource

    _soft, hard = resource.getrlimit(resource.RLIMIT_NPROC)
    limit = int(ceiling)
    infinity = getattr(resource, "RLIM_INFINITY", -1)
    if hard != infinity and limit > int(hard):
        limit = int(hard)
    resource.setrlimit(resource.RLIMIT_NPROC, (limit, limit))


def _extend_path() -> None:
    """Use only the staged package directories the parent resolved.

    ``-S`` already skipped site-packages. These entries are inserted in the
    parent's order, in front of the standard library, and the file is removed
    before the strategy is loaded.
    """
    path = Path("deps.json")
    if not path.is_file():
        return
    spec = json.loads(path.read_text(encoding="utf-8"))
    path.unlink()
    entries = spec.get("path") if isinstance(spec, dict) else spec
    entries = [item for item in (entries or []) if item]
    for entry in reversed(entries):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    if os.name == "nt" and hasattr(os, "add_dll_directory"):
        # The real directory the parent imported. add_dll_directory rejects
        # the junction in the slot (WinError 87) and wants this path.
        dlls = spec.get("dlls") if isinstance(spec, dict) else None
        for raw in dlls or []:
            try:
                folder = Path(raw).resolve()
            except OSError:
                continue
            if not folder.is_dir():
                continue
            try:
                os.add_dll_directory(str(folder))
            except OSError:
                continue


def _windows_token_facts() -> dict:
    """AppContainer facts for this process. Empty capabilities are required."""
    import ctypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.OpenProcessToken.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
    advapi.OpenProcessToken.restype = ctypes.c_int
    advapi.GetTokenInformation.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    advapi.GetTokenInformation.restype = ctypes.c_int
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    facts = {"is_app_container": 0, "capability_count": -1, "error": ""}
    token = ctypes.c_void_p()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        facts["error"] = f"OpenProcessToken winerror {ctypes.get_last_error()}"
        return facts
    try:
        flag = ctypes.c_uint32()
        needed = ctypes.c_uint32()
        if not advapi.GetTokenInformation(token, 29, ctypes.byref(flag), 4, ctypes.byref(needed)):
            facts["error"] = f"TokenIsAppContainer winerror {ctypes.get_last_error()}"
            return facts
        facts["is_app_container"] = int(flag.value)
        if not advapi.GetTokenInformation(token, 30, None, 0, ctypes.byref(needed)) and not needed.value:
            facts["error"] = f"TokenCapabilities winerror {ctypes.get_last_error()}"
            return facts
        size = int(needed.value or 4)
        buf = ctypes.create_string_buffer(size)
        if not advapi.GetTokenInformation(token, 30, buf, size, ctypes.byref(needed)):
            facts["error"] = f"TokenCapabilities winerror {ctypes.get_last_error()}"
            return facts
        facts["capability_count"] = int(ctypes.c_uint32.from_buffer_copy(buf).value)
        return facts
    finally:
        kernel.CloseHandle(token)


def _refuse_if_unconfined() -> str | None:
    """A Windows child that is not an empty AppContainer does not run the strategy."""
    if os.name != "nt":
        return None
    facts = _windows_token_facts()
    try:
        Path("token.json").write_text(json.dumps(facts), encoding="utf-8")
    except OSError:
        pass
    sys.stderr.write("token " + json.dumps(facts) + "\n")
    sys.stderr.flush()
    if facts.get("is_app_container") != 1:
        return "the child is not an AppContainer"
    if facts.get("capability_count") not in (0,):
        return "the AppContainer has capabilities"
    return None


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
        confined = _refuse_if_unconfined()
        if confined:
            _recv()
            _send({"op": "error", "error": confined})
            return 1
        try:
            strategy, wants, has_day_end = _load()
        except BaseException as exc:
            # Wait for hello so the error is the reply, not a write into a
            # pipe the parent has not opened yet.
            _recv()
            _send(_exception_error(exc))
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
                except BaseException as exc:
                    _send(_exception_error(exc))
                    return 1
                _send({"op": "decision", "target": _encode(target)})
                continue
            if op == "day_end":
                try:
                    hook = getattr(strategy, "on_day_end", None)
                    if callable(hook):
                        hook(message.get("summary") or {})
                except BaseException as exc:
                    _send(_exception_error(exc))
                    return 1
                _send({"op": "decision", "target": None})
                continue
            _send({"op": "error", "error": f"unknown op {op!r}"})
            return 2
    except BrokenPipeError:
        return 0
    except BaseException as exc:
        try:
            _send(_exception_error(exc))
        except Exception:
            pass
        return 1
    finally:
        _finish_stdio()


if __name__ == "__main__":
    sys.exit(main())
