"""OS jail for the referee child.

Windows: a job object (kill on close, one active process, a memory cap, a CPU
cap), an AppContainer with no network capability, and file ACL deny entries on
the data, market, and repository directories. The private temp directory is
the only writable location. If any of that cannot be applied, the launch
raises and the strategy is not started.

Linux: bubblewrap when ``bwrap`` can start a throwaway namespace. Otherwise
landlock plus a seccomp filter, rlimits, and ``PR_SET_PDEATHSIG``. The report
from ``describe_isolation`` is the guarantee that was actually applied.

``lock_worker_session`` is the smaller filter used by the walk-forward and
parallel workers after they become session leaders. It denies ``setsid`` and
``setpgid`` so a grandchild cannot leave the process group.
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import signal
import site
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path


class SandboxError(RuntimeError):
    """The jail could not be applied. The strategy was not started."""


# seccomp / BPF
_BPF_LD = 0x00
_BPF_W = 0x00
_BPF_ABS = 0x20
_BPF_JMP = 0x05
_BPF_JEQ = 0x10
_BPF_RET = 0x06
_BPF_ALU = 0x04
_BPF_AND = 0x50
_BPF_K = 0x00
_LD_W_ABS = _BPF_LD | _BPF_W | _BPF_ABS
_JMP_JEQ = _BPF_JMP | _BPF_JEQ | _BPF_K
_RET_K = _BPF_RET | _BPF_K
_ALU_AND = _BPF_ALU | _BPF_AND | _BPF_K
_SECCOMP_RET_ALLOW = 0x7FFF0000
_SECCOMP_RET_ERRNO = 0x00050000
_SECCOMP_RET_KILL = 0x80000000
_EPERM = 1
_AUDIT_ARCH_X86_64 = 0xC000003E
_PR_SET_PDEATHSIG = 1
_PR_SET_NO_NEW_PRIVS = 38
_PR_SET_SECCOMP = 22
_SECCOMP_MODE_FILTER = 2
_CLONE_THREAD = 0x00010000

# x86_64 syscall numbers. Other machines fail closed rather than guess.
_X86_64_NR = {
    "socket": 41,
    "connect": 42,
    "accept": 43,
    "sendto": 44,
    "recvfrom": 45,
    "sendmsg": 46,
    "recvmsg": 47,
    "shutdown": 48,
    "bind": 49,
    "listen": 50,
    "socketpair": 53,
    "clone": 56,
    "fork": 57,
    "vfork": 58,
    "ptrace": 101,
    "setpgid": 109,
    "setsid": 112,
    "unshare": 272,
    "accept4": 288,
    "setns": 308,
    "process_vm_readv": 310,
    "process_vm_writev": 311,
    "userfaultfd": 323,
    "clone3": 435,
    "pidfd_getfd": 438,
}

_SANDBOX_DENY = (
    "socket",
    "connect",
    "accept",
    "accept4",
    "sendto",
    "recvfrom",
    "sendmsg",
    "recvmsg",
    "shutdown",
    "bind",
    "listen",
    "socketpair",
    "fork",
    "vfork",
    "ptrace",
    "setpgid",
    "setsid",
    "unshare",
    "setns",
    "process_vm_readv",
    "process_vm_writev",
    "userfaultfd",
    "pidfd_getfd",
)

_LANDLOCK_HANDLED_FS = 0xFFFF
_LANDLOCK_READ = (1 << 0) | (1 << 2) | (1 << 3)  # execute, read file, read dir
_LANDLOCK_WRITE = _LANDLOCK_READ | (1 << 1) | (1 << 4) | (1 << 5) | (1 << 7) | (1 << 8) | (1 << 13) | (1 << 14)


class _SockFilter(ctypes.Structure):
    _fields_ = (
        ("code", ctypes.c_uint16),
        ("jt", ctypes.c_uint8),
        ("jf", ctypes.c_uint8),
        ("k", ctypes.c_uint32),
    )


class _SockFprog(ctypes.Structure):
    _fields_ = (
        ("len", ctypes.c_uint16),
        ("filter", ctypes.POINTER(_SockFilter)),
    )


def _insn(code: int, jt: int, jf: int, k: int) -> bytes:
    return struct.pack("<HBBI", code, jt, jf, k)


def _arch_and_numbers():
    machine = os.uname().machine.lower() if hasattr(os, "uname") else ""
    if machine in {"x86_64", "amd64"}:
        return _AUDIT_ARCH_X86_64, _X86_64_NR
    raise SandboxError(
        f"seccomp is not implemented for {machine or sys.platform}; refusing to run without it"
    )


def _filter_bytes(*, deny: tuple[str, ...], allow_threads: bool) -> bytes:
    arch, numbers = _arch_and_numbers()
    program = [
        _insn(_LD_W_ABS, 0, 0, 4),
        _insn(_JMP_JEQ, 1, 0, arch),
        _insn(_RET_K, 0, 0, _SECCOMP_RET_KILL),
        _insn(_LD_W_ABS, 0, 0, 0),
    ]
    for name in deny:
        program.append(_insn(_JMP_JEQ, 0, 1, numbers[name]))
        program.append(_insn(_RET_K, 0, 0, _SECCOMP_RET_ERRNO | _EPERM))
    if allow_threads:
        # clone is allowed only when CLONE_THREAD is set, so a thread can
        # start and a process cannot. clone3 is not listed: glibc uses it for
        # threads, and classic BPF cannot read the flag struct it points at.
        clone = numbers["clone"]
        program.extend(
            (
                _insn(_JMP_JEQ, 0, 4, clone),
                _insn(_LD_W_ABS, 0, 0, 16),
                _insn(_ALU_AND, 0, 0, _CLONE_THREAD),
                _insn(_JMP_JEQ, 1, 0, _CLONE_THREAD),
                _insn(_RET_K, 0, 0, _SECCOMP_RET_ERRNO | _EPERM),
            )
        )
    program.append(_insn(_RET_K, 0, 0, _SECCOMP_RET_ALLOW))
    return b"".join(program)


def _install_filter(blob: bytes) -> None:
    count = len(blob) // 8
    filters = (_SockFilter * count).from_buffer_copy(blob)
    prog = _SockFprog(count, filters)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        err = ctypes.get_errno()
        raise SandboxError(f"PR_SET_NO_NEW_PRIVS failed (errno {err})")
    if libc.prctl(_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER, ctypes.byref(prog)) != 0:
        err = ctypes.get_errno()
        raise SandboxError(f"seccomp filter failed (errno {err})")
    # Keep the buffer alive until prctl has copied it.
    del filters, prog


def lock_worker_session() -> None:
    """Deny setsid and setpgid in this process and every process it starts.

    The caller has already called ``setsid``. A later ``setsid`` returns
    EPERM, so a double fork cannot leave the process group. Windows does
    nothing here: the worker job object is the group. Non-Linux does nothing
    because there is no seccomp.
    """
    if sys.platform != "linux":
        return
    _install_filter(_filter_bytes(deny=("setsid", "setpgid"), allow_threads=False))


def _which(name: str) -> str | None:
    return shutil.which(name)


def _bwrap_available() -> bool:
    binary = _which("bwrap")
    if not binary:
        return False
    command = [
        binary,
        "--unshare-all",
        "--unshare-user",
        "--disable-userns",
        "--die-with-parent",
    ]
    for raw in ("/usr", "/lib", "/lib64"):
        if Path(raw).exists():
            command.extend(["--ro-bind", raw, raw])
    command.extend(["--proc", "/proc", "--dev", "/dev", "--", "/usr/bin/true"])
    try:
        probe = subprocess.run(
            command,
            capture_output=True,
            timeout=15,
            env={"PATH": "/usr/bin:/bin"},
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


def _landlock_available() -> bool:
    if sys.platform != "linux":
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        version = libc.syscall(444, None, 0, 1)
    except OSError:
        return False
    return version >= 1


def describe_isolation() -> dict:
    """What a launch on this machine will apply. Nothing is started."""
    if sys.platform == "win32":
        return _windows_guarantees()
    if sys.platform == "linux" and _bwrap_available():
        return _bwrap_guarantees()
    if sys.platform == "linux" and _landlock_available():
        return _landlock_guarantees()
    return {
        "os": sys.platform,
        "backend": None,
        "fail_closed": True,
        "note": "no bubblewrap, unshare jail, or landlock; a launch is refused",
    }


def _bwrap_guarantees() -> dict:
    return {
        "os": "linux",
        "backend": "bubblewrap",
        "network": (
            "new network namespace, and seccomp returns EPERM for socket, connect, "
            "and the rest of the socket calls"
        ),
        "filesystem": (
            "new mount namespace. The Python runtime and the private temp directory "
            "are mounted. Data, market, and repository directories are not. The child "
            "is python -I -S with a staged sys.path of the packages this process "
            "imports. site-packages metadata is not on that path. /etc/ld.so.cache "
            "is the only file bound from /etc."
        ),
        "process": (
            "new pid namespace and die-with-parent. seccomp denies setsid, setpgid, "
            "fork, vfork, and clone without CLONE_THREAD. clone3 stays allowed because "
            "glibc creates threads with it and classic BPF cannot read its flag pointer. "
            "A clone3 task inherits the filter, the mount namespace, and the network "
            "namespace, and it dies with the pid namespace."
        ),
        "parent_memory": (
            "the host /proc is not mounted. ptrace and process_vm_readv return EPERM"
        ),
        "memory": "RLIMIT_AS and RLIMIT_DATA hard caps, set before the strategy is loaded",
        "cpu": "RLIMIT_CPU hard cap, plus the parent's wall-clock per-call timeout",
        "fail_closed": True,
    }


def _landlock_guarantees() -> dict:
    return {
        "os": "linux",
        "backend": "landlock+seccomp",
        "network": "seccomp returns EPERM for socket and connect. A network namespace is not created.",
        "filesystem": (
            "landlock allow-list: read the Python runtime and the staged packages, "
            "write the private temp directory. Opening a data or repository path returns "
            "EPERM. This is not a mount namespace. The child is python -I -S, so "
            "site-packages is not on sys.path. Landlock rules are directories, so a "
            "prefix that contains site-packages cannot have that one directory punched out."
        ),
        "process": (
            "seccomp denies setsid, setpgid, fork, vfork, and clone without CLONE_THREAD. "
            "PR_SET_PDEATHSIG is SIGKILL. There is no pid namespace, so a raw clone3 task "
            "can outlive the strategy process if it is not killed with it. bubblewrap is "
            "the stronger backend and is used when it runs."
        ),
        "parent_memory": "/proc is not in the allow-list, so /proc/<pid>/mem cannot be opened. ptrace is denied.",
        "memory": "RLIMIT_AS and RLIMIT_DATA hard caps",
        "cpu": "RLIMIT_CPU hard cap, plus the parent's wall-clock per-call timeout",
        "fail_closed": True,
    }


def _windows_guarantees() -> dict:
    return {
        "os": "windows",
        "backend": "job+appcontainer",
        "network": (
            "AppContainer with no capabilities and no loopback exemption. A connect to a "
            "listening loopback server and to an external address both fail with access denied."
        ),
        "filesystem": (
            "The process token is an AppContainer, so the user SID does not grant file "
            "access. The profile is granted read/execute on the runtime once, not on every "
            "launch, and full control of the private temp directory. Data, market, and "
            "repository trees are not granted. LOCALAPPDATA is the host value the profile "
            "needs, and a non-inheriting deny on that directory is applied once. A child "
            "that is not an AppContainer is not resumed."
        ),
        "process": (
            "job object with KILL_ON_JOB_CLOSE, active process limit 1, a commit-charge cap, "
            "and a per-process user-time cap. The process is assigned while it is suspended."
        ),
        "parent_memory": (
            "the AppContainer token cannot open the parent with PROCESS_VM_READ"
        ),
        "memory": "JOB_OBJECT_LIMIT_PROCESS_MEMORY and JOB_OBJECT_LIMIT_JOB_MEMORY",
        "cpu": "JOB_OBJECT_LIMIT_PROCESS_TIME, plus the parent's wall-clock per-call timeout",
        "fail_closed": True,
    }


def _host_children(pid: int) -> list[int]:
    path = Path(f"/proc/{pid}/task/{pid}/children")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    children = []
    for item in text.split():
        try:
            children.append(int(item))
        except ValueError:
            continue
    return children


def _kill_tree(pid: int) -> None:
    """SIGKILL a process and the host-visible processes under it.

    The inner sandbox calls setsid, so it is not in the launcher's group.
    die-with-parent covers that case. Walking the children covers a process
    that is still alive when the launcher has just been signaled.
    """
    seen = set()

    def walk(current: int) -> None:
        if current in seen or current <= 0:
            return
        seen.add(current)
        for child in _host_children(current):
            walk(child)
        for sig_target in (current,):
            try:
                os.kill(sig_target, signal.SIGKILL)
            except OSError:
                pass

    for _ in range(3):
        walk(pid)
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass
        if not _host_children(pid):
            break


class Sandbox:
    """A running jailed interpreter. ``close`` kills it and removes the temp dir."""

    def __init__(self, process, workdir: Path, backend: str, guarantees: dict, cleanup):
        self.process = process
        self.workdir = workdir
        self.backend = backend
        self.guarantees = guarantees
        self.stdin_fd = process.stdin.fileno()
        self.stdout_fd = process.stdout.fileno()
        self.stderr_fd = process.stderr.fileno()
        self._cleanup = cleanup
        self._closed = False

    def terminate(self) -> None:
        proc = self.process
        if proc.poll() is not None:
            return
        if os.name != "nt":
            _kill_tree(proc.pid)
        else:
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                return

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.terminate()
        finally:
            for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
                try:
                    if pipe is not None:
                        pipe.close()
                except OSError:
                    pass
            cleanup = self._cleanup
            self._cleanup = None
            if cleanup is not None:
                cleanup()
            shutil.rmtree(self.workdir, ignore_errors=True)


def _interpreter_command(script: Path, *, child: bool = False) -> list[str]:
    """How to execute a jail script with this interpreter.

    A source child is ``python -I -S -B``. ``-S`` skips site.py, so the
    editable install and dist-packages are not added to ``sys.path``. The
    parent then inserts only the staged package directories. A frozen child
    is this executable again, with ``--sandbox-child`` and the private
    directory as its cwd. No strategy path is put on that command line.
    """
    if child and getattr(sys, "frozen", False):
        return [sys.executable, "--sandbox-child"]
    if getattr(sys, "frozen", False):
        return [sys.executable, str(script)]
    if child:
        return [sys.executable, "-I", "-S", "-B", str(script)]
    return [sys.executable, "-I", "-S", "-B", str(script)]


def launch(
    strategy_path,
    *,
    memory_bytes: int,
    cpu_seconds: int,
    deny_paths,
    capture_stdio: bool = False,
) -> Sandbox:
    """Copy the strategy into a private directory and start it inside the jail.

    ``deny_paths`` are host directories the child must not open. They are not
    passed on the command line of the child.
    """
    if not isinstance(memory_bytes, int) or isinstance(memory_bytes, bool) or memory_bytes <= 0:
        raise ValueError("memory cap must be a positive number of bytes")
    if not isinstance(cpu_seconds, int) or isinstance(cpu_seconds, bool) or cpu_seconds <= 0:
        raise ValueError("cpu cap must be a positive number of seconds")
    strategy = Path(strategy_path).resolve()
    if not strategy.is_file():
        raise FileNotFoundError(f"strategy file not found: {strategy}")
    denied = _deny_roots(deny_paths)
    work = Path(_private_dir())
    try:
        for banned in denied:
            if work == banned or banned in work.parents or work in banned.parents:
                raise SandboxError("the private directory overlaps a denied directory")
        _write_workspace(
            work,
            strategy,
            memory_bytes=memory_bytes,
            cpu_seconds=cpu_seconds,
            capture_stdio=capture_stdio,
        )
        if sys.platform == "win32":
            return _launch_windows(work, denied, memory_bytes=memory_bytes, cpu_seconds=cpu_seconds)
        if sys.platform != "linux":
            raise SandboxError(f"no referee jail for {sys.platform}; refusing to run the strategy")
        if _bwrap_available():
            return _launch_bwrap(work, denied, memory_bytes=memory_bytes, cpu_seconds=cpu_seconds)
        if _landlock_available():
            return _launch_landlock(work, denied, memory_bytes=memory_bytes, cpu_seconds=cpu_seconds)
        raise SandboxError(
            "bubblewrap is not available and landlock cannot be applied; refusing to run the strategy"
        )
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise


def _private_dir() -> str:
    import tempfile

    return tempfile.mkdtemp(prefix="bt-referee-")


def _deny_roots(paths) -> list[Path]:
    roots = []
    raw = list(paths or [])
    # A source tree sits outside the interpreter, so the child must not read
    # it. A frozen bundle is the interpreter: ``parents[2]`` is the install
    # directory, and denying it overlaps the runtime grant.
    if not getattr(sys, "frozen", False):
        raw.append(Path(__file__).resolve().parents[2])
    for item in raw:
        if not item:
            continue
        path = Path(item).resolve()
        if path not in roots:
            roots.append(path)
    return roots


def _write_workspace(
    work: Path,
    strategy: Path,
    *,
    memory_bytes: int,
    cpu_seconds: int,
    capture_stdio: bool,
) -> None:
    shim = Path(__file__).resolve().parent / "sandbox_child.py"
    (work / "strategy.py").write_bytes(strategy.read_bytes())
    # A frozen jail is the executable itself. There is no system python and
    # no .py shim on disk. Source runs still copy the shim.
    if not getattr(sys, "frozen", False):
        if not shim.is_file():
            raise SandboxError("sandbox_child.py is missing")
        (work / "sandbox_child.py").write_bytes(shim.read_bytes())
    limits = {
        "memory_bytes": int(memory_bytes),
        "cpu_seconds": int(cpu_seconds),
        "capture_stdio": bool(capture_stdio),
    }
    (work / "limits.json").write_text(json.dumps(limits), encoding="utf-8")
    _stage_packages(work)


def _runtime_roots() -> list[Path]:
    """Directories the dynamic linker and the interpreter open.

    ``/lib`` and ``/lib64`` stay even when they are symlinks into ``/usr``.
    The sandbox has to have those paths, not only the directory they point at.
    """
    roots: list[Path] = []

    def add(path: Path, *, fold: bool) -> None:
        if not path.exists():
            return
        if any(path == earlier for earlier in roots):
            return
        if fold and any(path == earlier or earlier in path.parents for earlier in roots):
            return
        roots.append(path)

    for raw in ("/usr", "/lib", "/lib64", "/bin"):
        add(Path(os.path.abspath(raw)), fold=False)
    for raw in (sys.base_prefix, sys.prefix):
        if raw:
            add(Path(raw).resolve(), fold=True)
    add(Path(sys.executable).resolve().parent, fold=True)
    return roots


def _site_dirs() -> list[Path]:
    found = []
    try:
        candidates = list(site.getsitepackages())
    except Exception:
        candidates = []
    try:
        user = site.getusersitepackages()
    except Exception:
        user = None
    if user:
        candidates.append(user)
    for raw in candidates:
        path = Path(raw).resolve()
        if path.is_dir() and path not in found:
            found.append(path)
    return found


# Imported in the parent so the staged copy is the one this process uses,
# in this order. A name that is not installed is skipped.
_STAGED_IMPORTS = ("numpy", "pandas", "dateutil", "pytz", "tzdata", "six")


def _runtime_package_dirs() -> list[Path]:
    """Directories and modules this process actually imported.

    Each path is the package directory (or a single-file module), never the
    site-packages directory that holds ``*.pth`` and ``direct_url.json``.
    A sibling ``*.libs`` directory is included so a wheel's OpenBLAS loads.
    Anything inside this repository is not staged.
    """
    import importlib

    repo = Path(__file__).resolve().parents[2]
    found: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        try:
            resolved = path.resolve()
        except OSError:
            return
        if resolved in seen or not resolved.exists():
            return
        if resolved == repo or repo in resolved.parents:
            return
        seen.add(resolved)
        found.append(resolved)

    for name in _STAGED_IMPORTS:
        try:
            module = importlib.import_module(name)
        except Exception:
            continue
        file = getattr(module, "__file__", None)
        if not file:
            continue
        module_path = Path(file)
        if module_path.name == "__init__.py":
            package = module_path.parent
            add(package)
            sibling = package.parent / f"{package.name}.libs"
            if sibling.is_dir():
                add(sibling)
        else:
            add(module_path)
    return found


def _link_into(src: Path, dest: Path) -> None:
    """Point ``dest`` at ``src`` without copying a package tree."""
    if dest.exists() or dest.is_symlink():
        return
    if src.is_file() or os.name != "nt":
        try:
            os.symlink(src, dest, target_is_directory=src.is_dir())
            return
        except OSError:
            if src.is_file():
                shutil.copy2(src, dest)
                return
            raise
    proc = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(dest), str(src)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise SandboxError(f"could not stage {src.name}: {detail}")


def _stage_packages(work: Path) -> list[Path]:
    """Build ``pydeps/NN`` slots in parent import order and write ``deps.json``.

    The slot holds one package. Its parent is what goes on ``sys.path``, so a
    scan of that path cannot see a sibling ``dist-info`` or ``.pth``. A
    ``*.libs`` directory is placed in that same slot: on Windows the numpy
    loader looks beside the package, not on ``sys.path``.
    """
    packages = _runtime_package_dirs()
    stage = work / "pydeps"
    stage.mkdir()
    entries = []
    dlls = []
    slots: dict[Path, Path] = {}
    index = 0
    for path in packages:
        if path.name.endswith(".libs"):
            owner = path.parent / path.name[: -len(".libs")]
            slot = slots.get(owner)
            if slot is not None:
                _link_into(path, slot / path.name)
                dlls.append(str(path))
                continue
        slot = stage / f"{index:02d}"
        slot.mkdir()
        _link_into(path, slot / path.name)
        slots[path] = slot
        entries.append(f"pydeps/{index:02d}")
        index += 1
        if path.name.endswith(".libs"):
            dlls.append(str(path))
    (work / "deps.json").write_text(
        json.dumps({"path": entries, "dlls": dlls}),
        encoding="utf-8",
    )
    return packages


def _child_env(work: Path) -> dict:
    env = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "HOME": str(work),
        "TMPDIR": str(work),
        "TEMP": str(work),
        "TMP": str(work),
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    return env


def _launch_bwrap(work: Path, denied: list[Path], *, memory_bytes: int, cpu_seconds: int) -> Sandbox:
    binary = _which("bwrap")
    if not binary:
        raise SandboxError("bubblewrap disappeared before launch")
    roots = _runtime_roots()
    for root in roots:
        for banned in denied:
            if root == banned or banned in root.parents:
                raise SandboxError(
                    f"the Python runtime {root} is inside a denied directory; refusing to mount it"
                )
    cmd = [
        binary,
        "--unshare-all",
        "--unshare-user",
        "--disable-userns",
        "--die-with-parent",
        "--new-session",
        "--cap-drop",
        "ALL",
    ]
    for root in roots:
        cmd.extend(["--ro-bind", str(root), str(root)])
    packages = _runtime_package_dirs()
    # A prefix mount includes site-packages. Cover that directory, then put
    # back only the package directories the parent resolved. Metadata files
    # that sit beside them are not mounted. A package outside the prefix is
    # bound on its own path, so its parent in the jail contains only that bind.
    hidden_sites = []
    for directory in _site_dirs():
        covered = any(directory == root or root in directory.parents for root in roots)
        if covered and directory not in hidden_sites:
            hidden_sites.append(directory)
            cmd.extend(["--tmpfs", str(directory)])
    for path in packages:
        cmd.extend(["--ro-bind", str(path), str(path)])
    cache = Path("/etc/ld.so.cache")
    if cache.is_file():
        cmd.extend(["--dir", "/etc", "--ro-bind", str(cache), str(cache)])
    cmd.extend(["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--bind", str(work), str(work)])
    pass_fds = []
    try:
        filt = _filter_bytes(deny=_SANDBOX_DENY, allow_threads=True)
        seccomp_fd = os.memfd_create("sb-seccomp", 0)
        os.write(seccomp_fd, filt)
        os.lseek(seccomp_fd, 0, os.SEEK_SET)
        pass_fds.append(seccomp_fd)
        cmd.extend(
            [
                "--seccomp",
                str(seccomp_fd),
                "--chdir",
                str(work),
                "--clearenv",
            ]
        )
        for key, value in _child_env(work).items():
            cmd.extend(["--setenv", key, value])
        cmd.extend(_interpreter_command(work / "sandbox_child.py", child=True))
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                pass_fds=tuple(pass_fds),
                start_new_session=True,
                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                cwd=str(work),
            )
        except OSError as exc:
            raise SandboxError(f"bubblewrap did not start: {exc}") from exc
    finally:
        for fd in pass_fds:
            os.close(fd)

    def _cleanup():
        return None

    return Sandbox(proc, work, "bubblewrap", _bwrap_guarantees(), _cleanup)


def _launch_landlock(work: Path, denied: list[Path], *, memory_bytes: int, cpu_seconds: int) -> Sandbox:
    """Landlock, seccomp, rlimits, and a parent-death signal. No preexec fork."""
    read_dirs = [str(path) for path in _runtime_roots()]
    for package in _runtime_package_dirs():
        directory = package if package.is_dir() else package.parent
        if str(directory) not in read_dirs:
            read_dirs.append(str(directory))
    cache = Path("/etc/ld.so.cache")
    if cache.is_file():
        # Landlock names directories. The cache file's directory is the
        # smallest rule that lets the linker open that one file.
        etc = str(cache.parent)
        if etc not in read_dirs:
            read_dirs.append(etc)
    for directory in read_dirs:
        path = Path(directory)
        for banned in denied:
            if path == banned or banned in path.parents:
                raise SandboxError(
                    "the Python runtime is inside a denied directory; refusing to grant it"
                )
    rules = []
    for directory in read_dirs:
        if directory and os.path.isdir(directory):
            rules.append([directory, _LANDLOCK_READ])
    rules.append([str(work), _LANDLOCK_WRITE])
    (work / "filter.bin").write_bytes(_filter_bytes(deny=_SANDBOX_DENY, allow_threads=True))
    (work / "jail.json").write_text(
        json.dumps(
            {
                "handled": _LANDLOCK_HANDLED_FS,
                "rules": rules,
                "memory_bytes": int(memory_bytes),
                "cpu_seconds": int(cpu_seconds),
            }
        ),
        encoding="utf-8",
    )
    (work / "enter.py").write_text(_LANDLOCK_ENTER, encoding="utf-8")
    try:
        proc = subprocess.Popen(
            _interpreter_command(work / "enter.py", child=True),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            cwd=str(work),
            env=_child_env(work),
            start_new_session=True,
        )
    except OSError as exc:
        raise SandboxError(f"landlock jail did not start: {exc}") from exc
    return Sandbox(proc, work, "landlock+seccomp", _landlock_guarantees(), lambda: None)


_LANDLOCK_ENTER = r"""
import ctypes
import json
import os
import signal
import sys
from pathlib import Path


def _fail(message):
    raise SystemExit(message)


def main():
    spec = json.loads(Path("jail.json").read_text(encoding="utf-8"))
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, int(signal.SIGKILL), 0, 0, 0) != 0:
        _fail("parent-death signal was not installed")

    class Attr(ctypes.Structure):
        _fields_ = (("handled_access_fs", ctypes.c_uint64),)

    class Rule(ctypes.Structure):
        _pack_ = 1
        _fields_ = (("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32))

    attr = Attr(int(spec["handled"]))
    ruleset = libc.syscall(444, ctypes.byref(attr), ctypes.sizeof(attr), 0)
    if ruleset < 0:
        _fail("landlock_create_ruleset failed")
    try:
        for directory, access in spec["rules"]:
            if not directory or not os.path.isdir(directory):
                continue
            raw = os.open(directory, os.O_PATH | os.O_CLOEXEC)
            try:
                rule = Rule(int(access), raw)
                if libc.syscall(445, ruleset, 1, ctypes.byref(rule), 0) != 0:
                    _fail("landlock_add_rule failed")
            finally:
                os.close(raw)
        if libc.prctl(38, 1, 0, 0, 0) != 0:
            _fail("no-new-privs was not set")
        if libc.syscall(446, ruleset, 0) != 0:
            _fail("landlock_restrict_self failed")
    finally:
        os.close(ruleset)

    import resource

    memory = int(spec["memory_bytes"])
    cpu = int(spec["cpu_seconds"])
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    resource.setrlimit(resource.RLIMIT_DATA, (memory, memory))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))

    blob = Path("filter.bin").read_bytes()

    class SockFilter(ctypes.Structure):
        _fields_ = (
            ("code", ctypes.c_uint16),
            ("jt", ctypes.c_uint8),
            ("jf", ctypes.c_uint8),
            ("k", ctypes.c_uint32),
        )

    class SockFprog(ctypes.Structure):
        _fields_ = (("len", ctypes.c_uint16), ("filter", ctypes.POINTER(SockFilter)))

    count = len(blob) // 8
    filters = (SockFilter * count).from_buffer_copy(blob)
    prog = SockFprog(count, filters)
    if libc.prctl(22, 2, ctypes.byref(prog)) != 0:
        _fail("seccomp filter was not installed")
    for name in ("jail.json", "filter.bin"):
        try:
            os.unlink(name)
        except OSError:
            _fail("jail metadata was left in the work directory")
    if getattr(sys, "frozen", False):
        os.execv(sys.executable, [sys.executable, "--sandbox-child"])
    os.execv(sys.executable, [sys.executable, "-I", "-S", "-B", "sandbox_child.py"])


if __name__ == "__main__":
    main()
"""


def _launch_windows(work: Path, denied: list[Path], *, memory_bytes: int, cpu_seconds: int) -> Sandbox:
    """Job object, AppContainer with no capabilities, and file ACLs. Fail closed."""
    if sys.platform != "win32":
        raise SandboxError("the Windows jail is only applied on Windows")
    lease = _win_lease_acquire()
    extra_grants: list[tuple[Path, bool]] = []
    popen = None
    try:
        try:
            _win_reject_overlap(lease.grant_paths, denied)
            # The AppContainer token is the file boundary. A per-launch
            # recursive deny of the repository walks the tree inside the
            # strategy timeout. The private directory is the only grant
            # that changes per launch, and it is a handful of files.
            extra_grants = _win_grant_work(lease, work)
            popen = _win_spawn(work, lease, memory_bytes=memory_bytes, cpu_seconds=cpu_seconds)
        except Exception:
            _win_drop_launch_aces(lease, [], [], extra_grants)
            raise
    finally:
        if popen is None:
            _win_lease_release()

    def _cleanup():
        try:
            kernel = getattr(popen, "_kernel", None)
            job = getattr(popen, "_job", None)
            process = getattr(popen, "_process", None)
            if kernel is not None and job:
                kernel.CloseHandle(job)
                popen._job = None
            if kernel is not None and process:
                kernel.CloseHandle(process)
                popen._process = None
        finally:
            try:
                _win_drop_launch_aces(lease, [], [], extra_grants)
            finally:
                _win_lease_release()

    return Sandbox(popen, work, "job+appcontainer", _windows_guarantees(), _cleanup)


def _win_drop_launch_aces(lease, denies, shallow, extra_grants) -> None:
    _win_remove_aces(lease, denies, recurse=True)
    _win_remove_aces(lease, shallow, recurse=False)
    for path, recurse in extra_grants:
        _win_remove_aces(lease, [path], recurse=recurse)


class _WinLease:
    def __init__(self):
        self.sid = ctypes.c_void_p()
        self.sid_text = ""
        self.name = ""
        self.refs = 0
        self.grant_paths: list[tuple[Path, bool]] = []
        self.ancestor_grants: set[Path] = set()
        self.userenv = None
        self.advapi = None
        self.kernel = None


_WIN_LEASE: _WinLease | None = None
_WIN_LOCK = threading.Lock()


def _win_lease_acquire() -> _WinLease:
    global _WIN_LEASE
    with _WIN_LOCK:
        if _WIN_LEASE is None:
            _win_sweep_orphans()
            _WIN_LEASE = _WinLease()
            _win_lease_open(_WIN_LEASE)
        lease = _WIN_LEASE
        lease.refs += 1
        return lease


def _win_lease_release() -> None:
    """Drop one launch. The profile and the runtime grants stay until exit.

    Releasing them here made every test walk the Python tree with icacls
    again. Orphaned profiles from a dead process are removed on the next start.
    """
    with _WIN_LOCK:
        lease = _WIN_LEASE
        if lease is None or lease.refs <= 0:
            return
        lease.refs -= 1


def _win_fail(prefix: str) -> SandboxError:
    code = ctypes.get_last_error()
    return SandboxError(f"{prefix} (winerror {code})")


_PROFILE_NAME = "strategybuilderjail"
_ALREADY_EXISTS = 0x800700B7


def warm_windows_profile() -> None:
    """Create the AppContainer and its runtime ACLs once per machine.

    The first jail in a process pays for ``icacls`` on the interpreter.
    Later launches, including workers spawned after this returns, reuse the
    marker and only grant their private directory.
    """
    if sys.platform != "win32":
        return
    lease = _win_lease_acquire()
    _win_lease_release()
    return lease


def _grant_marker_path() -> Path:
    local = _local_app_data()
    if local is None:
        raise SandboxError("LOCALAPPDATA is not set; refusing to start the sandbox")
    return local / "strategybuilderjail-grants.json"


def _with_profile_lock(action):
    local = _local_app_data()
    if local is None:
        raise SandboxError("LOCALAPPDATA is not set; refusing to start the sandbox")
    lock = local / "strategybuilderjail.lock"
    deadline = time.monotonic() + 300
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > 600:
                    lock.unlink()
                    continue
            except OSError:
                pass
            if time.monotonic() > deadline:
                raise SandboxError("timed out waiting for the AppContainer grant")
            time.sleep(0.25)
    try:
        return action()
    finally:
        os.close(fd)
        try:
            lock.unlink()
        except OSError:
            pass


def _win_block_network(sid_text: str) -> None:
    """Block every inbound and outbound connection for this package SID.

    An AppContainer with no capabilities still reaches loopback far enough
    for a connect to time out instead of failing with access denied. A
    firewall block on the package SID fails that connect with 10013.
    ``netsh`` on current Windows rejects ``package=``, so this uses the
    firewall cmdlet.
    """
    if not sid_text.startswith("S-1-") or any(ch in sid_text for ch in "\"'$`;\r\n"):
        raise SandboxError("the AppContainer SID is not a form the firewall rule can use")
    system = os.environ.get("SystemRoot", "")
    powershell = Path(system) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe" if system else None
    if powershell is None or not powershell.is_file():
        raise SandboxError("powershell is not available; refusing to run without a network block")
    command = (
        "$names = @('strategybuilderjail-block-out','strategybuilderjail-block-in')\n"
        "foreach ($name in $names) {\n"
        "  Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue |"
        " Remove-NetFirewallRule -ErrorAction SilentlyContinue\n"
        "}\n"
        f"New-NetFirewallRule -DisplayName 'strategybuilderjail-block-out' -Direction Outbound"
        f" -Action Block -Package '{sid_text}' -Profile Any -ErrorAction Stop | Out-Null\n"
        f"New-NetFirewallRule -DisplayName 'strategybuilderjail-block-in' -Direction Inbound"
        f" -Action Block -Package '{sid_text}' -Profile Any -ErrorAction Stop | Out-Null\n"
    )
    try:
        proc = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SandboxError(f"the AppContainer firewall block was not added: {exc}") from exc
    if proc.returncode != 0:
        detail = ((proc.stdout or "") + (proc.stderr or "")).strip()
        raise SandboxError(f"the AppContainer firewall block was not added: {detail[-500:]}")


def _clear_loopback(name: str, sid_text: str) -> None:
    """Drop a loopback exemption for this profile. Missing entries are fine."""
    system = os.environ.get("SystemRoot", "")
    if not system:
        return
    exe = Path(system) / "System32" / "CheckNetIsolation.exe"
    if not exe.is_file():
        return
    for args in (["-d", "-n=" + name], ["-d", "-p=" + sid_text]):
        try:
            subprocess.run(
                [str(exe), "LoopbackExempt", *args],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue


def _recorded_grants(sid_text: str):
    path = _grant_marker_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if payload.get("sid") != sid_text:
        return None
    recorded_roots = [os.path.normcase(str(item)) for item in payload.get("roots") or []]
    if recorded_roots != [os.path.normcase(str(path)) for path in _windows_grant_roots()]:
        return None
    if not payload.get("local_denied"):
        return None
    grants = []
    for item in payload.get("grants") or []:
        if not isinstance(item, list) or len(item) != 2:
            return None
        grants.append((Path(item[0]), bool(item[1])))
    return grants


def _write_grant_marker(sid_text: str, grants, *, local_denied: bool, loopback_cleared: bool) -> None:
    payload = {
        "sid": sid_text,
        "roots": [str(path) for path in _windows_grant_roots()],
        "grants": [[str(path), bool(recurse)] for path, recurse in grants],
        "local_denied": bool(local_denied),
        "loopback_cleared": bool(loopback_cleared),
        "firewall_blocked": True,
    }
    path = _grant_marker_path()
    path.write_text(json.dumps(payload), encoding="utf-8")


def _win_lease_open(lease: _WinLease) -> None:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    userenv = ctypes.WinDLL("userenv", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    userenv.CreateAppContainerProfile.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    userenv.CreateAppContainerProfile.restype = ctypes.c_long
    userenv.DeleteAppContainerProfile.argtypes = [ctypes.c_wchar_p]
    userenv.DeleteAppContainerProfile.restype = ctypes.c_long
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
    advapi.ConvertSidToStringSidW.restype = ctypes.c_int
    advapi.FreeSid.argtypes = [ctypes.c_void_p]
    advapi.FreeSid.restype = ctypes.c_void_p
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    userenv.DeriveAppContainerSidFromAppContainerName.argtypes = [
        ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    userenv.DeriveAppContainerSidFromAppContainerName.restype = ctypes.c_long
    name = _PROFILE_NAME
    sid = ctypes.c_void_p()
    created = False
    hr = userenv.CreateAppContainerProfile(name, name, name, None, 0, ctypes.byref(sid))
    if (hr & 0xFFFFFFFF) == _ALREADY_EXISTS or (hr != 0 and not sid.value):
        hr = userenv.DeriveAppContainerSidFromAppContainerName(name, ctypes.byref(sid))
        if hr != 0 or not sid.value:
            raise SandboxError(f"DeriveAppContainerSidFromAppContainerName failed (hresult {hr & 0xFFFFFFFF:08x})")
    elif hr != 0 or not sid.value:
        raise SandboxError(f"CreateAppContainerProfile failed (hresult {hr & 0xFFFFFFFF:08x})")
    else:
        created = True
    text_ptr = ctypes.c_wchar_p()
    if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(text_ptr)) or not text_ptr.value:
        advapi.FreeSid(sid)
        if created:
            userenv.DeleteAppContainerProfile(name)
        raise _win_fail("ConvertSidToStringSidW failed")
    sid_text = str(text_ptr.value)
    kernel.LocalFree(text_ptr)
    lease.sid = sid
    lease.sid_text = sid_text
    lease.name = name
    lease.userenv = userenv
    lease.advapi = advapi
    lease.kernel = kernel

    def ensure_grants():
        recorded = _recorded_grants(sid_text)
        marker = {}
        try:
            marker = json.loads(_grant_marker_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            marker = {}
        if (
            recorded is not None
            and marker.get("loopback_cleared")
            and marker.get("firewall_blocked")
            and marker.get("sid") == sid_text
        ):
            lease.grant_paths = recorded
            return
        # Once per machine. CheckNetIsolation and netsh can take longer than
        # a strategy timeout, so a later process must not run them again.
        _clear_loopback(name, sid_text)
        _win_block_network(sid_text)
        if recorded is not None:
            lease.grant_paths = recorded
            _write_grant_marker(
                sid_text, recorded, local_denied=True, loopback_cleared=True
            )
            return
        grants: list[tuple[Path, bool]] = []
        try:
            for root in _windows_grant_roots():
                recurse = root != _system_root()
                _win_grant(lease, root, "(OI)(CI)(RX)", recurse=recurse)
                grants.append((root, recurse))
                for parent in _ancestors(root):
                    if parent == root or any(parent == earlier for earlier, _flag in grants):
                        continue
                    _win_grant(lease, parent, "(X)", recurse=False)
                    grants.append((parent, False))
            local = _local_app_data()
            local_denied = False
            if local is not None and local.exists() and not _is_volume_root(local):
                _win_deny_shallow(lease, local)
                local_denied = True
        except Exception:
            lease.grant_paths = grants
            for path, recurse in grants:
                _win_remove_aces(lease, [path], recurse=recurse)
            raise
        lease.grant_paths = grants
        _write_grant_marker(
            sid_text, grants, local_denied=local_denied, loopback_cleared=True
        )

    try:
        _with_profile_lock(ensure_grants)
    except Exception:
        advapi.FreeSid(sid)
        if created:
            userenv.DeleteAppContainerProfile(name)
        raise


def _system_root() -> Path | None:
    raw = os.environ.get("SystemRoot")
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_dir() else None


def _windows_grant_roots() -> list[Path]:
    """Interpreter directories the AppContainer may read.

    User-site from ``site.getusersitepackages`` is not included. It depends
    on the process environment, so a scrubbed worker would miss the marker
    and walk the Python tree with ``icacls /T`` on every launch. A package
    that lives outside the prefix is added on its own.
    """
    roots: list[Path] = []
    frozen_root = getattr(sys, "_MEIPASS", None)
    candidates = [sys.base_prefix, sys.prefix, os.path.dirname(sys.executable), frozen_root]
    system = _system_root()
    if system is not None:
        candidates.append(str(system))
    for raw in candidates:
        if not raw:
            continue
        path = Path(raw).resolve()
        if path.is_dir() and path not in roots:
            roots.append(path)
    for package in _runtime_package_dirs():
        directory = package if package.is_dir() else package.parent
        if any(directory == root or root in directory.parents for root in roots):
            continue
        if directory.is_dir() and directory not in roots:
            roots.append(directory)
    roots.sort(key=lambda path: os.path.normcase(str(path)))
    return roots


def _is_volume_root(path: Path) -> bool:
    """True for ``/`` or ``C:\\``. icacls on a volume root does not finish."""
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    return resolved.parent == resolved


def _ancestors(path: Path) -> list[Path]:
    """Parents of ``path``, stopping before the volume root.

    The volume root already grants traverse to All Application Packages.
    Editing its ACL with icacls does not finish on a CI runner.
    """
    nodes = []
    current = path.resolve().parent
    while not _is_volume_root(current):
        nodes.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent
    return nodes


def _icacls() -> str:
    system = _system_root()
    if system is None:
        raise SandboxError("SystemRoot is not set; refusing to edit file ACLs")
    path = system / "System32" / "icacls.exe"
    if not path.is_file():
        raise SandboxError("icacls is not available; refusing to run without file ACLs")
    return str(path)


def _icacls_run(args: list[str]) -> None:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=180, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SandboxError(f"icacls did not finish: {exc}") from exc
    if proc.returncode != 0:
        detail = ((proc.stdout or "") + (proc.stderr or "")).strip()
        raise SandboxError(f"icacls failed ({proc.returncode}): {detail[-500:]}")


def _win_grant_traverse(lease: _WinLease, path: Path) -> None:
    """Add a non-inherited traverse ACE without walking the directory.

    ``icacls`` on ``C:\\hostedtoolcache\\windows`` does not return. The
    AppContainer only needs to cross that directory to reach Python.
    """
    if not path.exists():
        return
    advapi = lease.advapi
    kernel = lease.kernel

    class Trustee(ctypes.Structure):
        _fields_ = (
            ("pMultipleTrustee", ctypes.c_void_p),
            ("MultipleTrusteeOperation", ctypes.c_uint32),
            ("TrusteeForm", ctypes.c_uint32),
            ("TrusteeType", ctypes.c_uint32),
            ("ptstrName", ctypes.c_void_p),
        )

    class ExplicitAccess(ctypes.Structure):
        _fields_ = (
            ("grfAccessPermissions", ctypes.c_uint32),
            ("grfAccessMode", ctypes.c_uint32),
            ("grfInheritance", ctypes.c_uint32),
            ("Trustee", Trustee),
        )

    advapi.GetNamedSecurityInfoW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi.GetNamedSecurityInfoW.restype = ctypes.c_uint32
    advapi.SetEntriesInAclW.argtypes = [
        ctypes.c_uint32,
        ctypes.POINTER(ExplicitAccess),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi.SetEntriesInAclW.restype = ctypes.c_uint32
    advapi.SetNamedSecurityInfoW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    advapi.SetNamedSecurityInfoW.restype = ctypes.c_uint32
    dacl = ctypes.c_void_p()
    security = ctypes.c_void_p()
    # SE_FILE_OBJECT = 1, DACL_SECURITY_INFORMATION = 4
    status = advapi.GetNamedSecurityInfoW(str(path), 1, 4, None, None, ctypes.byref(dacl), None, ctypes.byref(security))
    if status != 0:
        raise SandboxError(f"could not read the ACL on {path} (winerror {status})")
    entry = ExplicitAccess()
    # FILE_TRAVERSE | FILE_READ_ATTRIBUTES | SYNCHRONIZE
    entry.grfAccessPermissions = 0x20 | 0x80 | 0x100000
    entry.grfAccessMode = 1  # GRANT_ACCESS
    entry.grfInheritance = 0
    entry.Trustee.TrusteeForm = 0  # TRUSTEE_IS_SID
    entry.Trustee.ptstrName = lease.sid
    updated = ctypes.c_void_p()
    try:
        status = advapi.SetEntriesInAclW(1, ctypes.byref(entry), dacl, ctypes.byref(updated))
        if status != 0:
            raise SandboxError(f"could not add a traverse ACE on {path} (winerror {status})")
        status = advapi.SetNamedSecurityInfoW(str(path), 1, 4, None, None, updated, None)
        if status != 0:
            raise SandboxError(f"could not write the ACL on {path} (winerror {status})")
    finally:
        if security.value:
            kernel.LocalFree(security)
        if updated.value:
            kernel.LocalFree(updated)


def _win_grant(lease: _WinLease, path: Path, rights: str, *, recurse: bool) -> None:
    if not path.exists():
        return
    if not recurse and rights == "(X)":
        _win_grant_traverse(lease, path)
        return
    command = [_icacls(), str(path), "/grant", f"*{lease.sid_text}:{rights}", "/C", "/Q"]
    if recurse:
        command.append("/T")
    _icacls_run(command)


def _win_deny(lease: _WinLease, path: Path) -> None:
    command = [_icacls(), str(path), "/deny", f"*{lease.sid_text}:(OI)(CI)(F)", "/T", "/C", "/Q"]
    _icacls_run(command)


def _win_deny_shallow(lease: _WinLease, path: Path) -> None:
    """Deny reading the directory itself. Children keep the ACEs they have."""
    command = [_icacls(), str(path), "/deny", f"*{lease.sid_text}:(R)", "/C", "/Q"]
    _icacls_run(command)


def _win_sweep_orphans() -> None:
    """Delete sb* AppContainer profiles left by a process that did not exit cleanly.

    A failure here does not open the jail. The new profile is still created,
    and a profile that cannot be deleted is unused once its process is gone.
    """
    if sys.platform != "win32":
        return
    try:
        import winreg
    except ImportError:
        return
    key_path = (
        r"Software\Classes\Local Settings\Software\Microsoft\Windows"
        r"\CurrentVersion\AppContainer\Mappings"
    )
    try:
        mapping = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path)
    except OSError:
        return
    orphans: list[tuple[str, str]] = []
    try:
        index = 0
        while True:
            try:
                sid_name = winreg.EnumKey(mapping, index)
            except OSError:
                break
            index += 1
            try:
                child = winreg.OpenKey(mapping, sid_name)
            except OSError:
                continue
            try:
                moniker = ""
                for value_name in ("Moniker", "DisplayName"):
                    try:
                        moniker, _kind = winreg.QueryValueEx(child, value_name)
                        break
                    except OSError:
                        continue
            finally:
                winreg.CloseKey(child)
            text = str(moniker)
            if len(text) == 18 and text.startswith("sb") and all(ch in "0123456789abcdef" for ch in text[2:]):
                orphans.append((text, sid_name))
    finally:
        winreg.CloseKey(mapping)
    if not orphans:
        return
    try:
        userenv = ctypes.WinDLL("userenv", use_last_error=True)
        userenv.DeleteAppContainerProfile.argtypes = [ctypes.c_wchar_p]
        userenv.DeleteAppContainerProfile.restype = ctypes.c_long
    except OSError:
        return
    # Removing ACEs with icacls /T walks the Python tree once per leftover
    # profile. The SIDs are unused once the profile is deleted. Leave the
    # ACEs; they do not grant a live process.
    for name, _sid_text in orphans:
        try:
            userenv.DeleteAppContainerProfile(name)
        except OSError:
            continue


def _win_remove_aces(lease: _WinLease, paths, *, recurse: bool) -> None:
    if lease is None or not lease.sid_text:
        return
    for path in paths:
        if not path or not Path(path).exists():
            continue
        command = [
            _icacls(),
            str(path),
            "/remove:g",
            f"*{lease.sid_text}",
            "/remove:d",
            f"*{lease.sid_text}",
            "/C",
            "/Q",
        ]
        if recurse:
            command.append("/T")
        try:
            _icacls_run(command)
        except SandboxError:
            # A path that disappeared is already gone. A real ACL failure still
            # has to surface: the next command raises if the ACE is still there
            # only when icacls itself fails. Keep the first failure.
            raise


def _win_reject_overlap(grants, denied) -> None:
    for banned in denied:
        if banned is None:
            continue
        banned_path = Path(banned)
        for grant, _recurse in grants:
            if grant == banned_path or banned_path in grant.parents:
                raise SandboxError(
                    "a denied directory contains the Python runtime; refusing to grant and deny the same tree"
                )


def _win_grant_work(lease: _WinLease, work: Path) -> list[tuple[Path, bool]]:
    applied: list[tuple[Path, bool]] = []
    _win_grant(lease, work, "(OI)(CI)(F)", recurse=True)
    applied.append((work, True))
    granted = {path for path, _recurse in lease.grant_paths}
    granted.update(lease.ancestor_grants)
    for parent in _ancestors(work):
        if parent in granted:
            continue
        _win_grant(lease, parent, "(X)", recurse=False)
        lease.ancestor_grants.add(parent)
        granted.add(parent)
    return applied


def _win_apply_denies(lease: _WinLease, denied) -> list[Path]:
    applied = []
    for banned in denied:
        if banned is None:
            continue
        path = Path(banned)
        if not path.exists():
            continue
        try:
            _win_deny(lease, path)
        except SandboxError:
            _win_remove_aces(lease, applied, recurse=True)
            raise
        applied.append(path)
    return applied


class _WinSecurityAttributes(ctypes.Structure):
    _fields_ = (
        ("nLength", ctypes.c_uint32),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", ctypes.c_int),
    )


class _WinStartupInfo(ctypes.Structure):
    _fields_ = (
        ("cb", ctypes.c_uint32),
        ("lpReserved", ctypes.c_wchar_p),
        ("lpDesktop", ctypes.c_wchar_p),
        ("lpTitle", ctypes.c_wchar_p),
        ("dwX", ctypes.c_uint32),
        ("dwY", ctypes.c_uint32),
        ("dwXSize", ctypes.c_uint32),
        ("dwYSize", ctypes.c_uint32),
        ("dwXCountChars", ctypes.c_uint32),
        ("dwYCountChars", ctypes.c_uint32),
        ("dwFillAttribute", ctypes.c_uint32),
        ("dwFlags", ctypes.c_uint32),
        ("wShowWindow", ctypes.c_uint16),
        ("cbReserved2", ctypes.c_uint16),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", ctypes.c_void_p),
        ("hStdOutput", ctypes.c_void_p),
        ("hStdError", ctypes.c_void_p),
    )


class _WinStartupInfoEx(ctypes.Structure):
    _fields_ = (("StartupInfo", _WinStartupInfo), ("lpAttributeList", ctypes.c_void_p))


class _WinSecurityCapabilities(ctypes.Structure):
    _fields_ = (
        ("AppContainerSid", ctypes.c_void_p),
        ("Capabilities", ctypes.c_void_p),
        ("CapabilityCount", ctypes.c_uint32),
        ("Reserved", ctypes.c_uint32),
    )


class _WinProcessInformation(ctypes.Structure):
    _fields_ = (
        ("hProcess", ctypes.c_void_p),
        ("hThread", ctypes.c_void_p),
        ("dwProcessId", ctypes.c_uint32),
        ("dwThreadId", ctypes.c_uint32),
    )


class _WinJobBasic(ctypes.Structure):
    _fields_ = (
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    )


class _WinJobIo(ctypes.Structure):
    _fields_ = tuple(
        (name, ctypes.c_uint64)
        for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    )


class _WinJobExtended(ctypes.Structure):
    _fields_ = (
        ("BasicLimitInformation", _WinJobBasic),
        ("IoInfo", _WinJobIo),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    )


def _win_spawn(work: Path, lease: _WinLease, *, memory_bytes: int, cpu_seconds: int):
    import msvcrt

    kernel = lease.kernel
    kernel.CreatePipe.argtypes = [
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    kernel.CreatePipe.restype = ctypes.c_int
    kernel.SetHandleInformation.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32]
    kernel.SetHandleInformation.restype = ctypes.c_int
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    kernel.ResumeThread.argtypes = [ctypes.c_void_p]
    kernel.ResumeThread.restype = ctypes.c_uint32
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    kernel.CreateJobObjectW.restype = ctypes.c_void_p
    kernel.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    kernel.SetInformationJobObject.restype = ctypes.c_int
    kernel.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel.AssignProcessToJobObject.restype = ctypes.c_int
    kernel.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.TerminateJobObject.restype = ctypes.c_int
    kernel.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.TerminateProcess.restype = ctypes.c_int
    kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    kernel.GetExitCodeProcess.restype = ctypes.c_int
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.WaitForSingleObject.restype = ctypes.c_uint32
    kernel.InitializeProcThreadAttributeList.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel.InitializeProcThreadAttributeList.restype = ctypes.c_int
    kernel.UpdateProcThreadAttribute.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel.UpdateProcThreadAttribute.restype = ctypes.c_int
    kernel.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    kernel.DeleteProcThreadAttributeList.restype = None
    kernel.CreateProcessW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    kernel.CreateProcessW.restype = ctypes.c_int

    if ctypes.sizeof(_WinJobExtended) != 144:
        raise SandboxError(
            f"job object layout is {ctypes.sizeof(_WinJobExtended)} bytes, not 144; refusing to apply a misread cap"
        )
    stdin_r, stdin_w = _win_pipe(kernel)
    stdout_r, stdout_w = _win_pipe(kernel)
    stderr_r, stderr_w = _win_pipe(kernel)
    for handle in (stdin_w, stdout_r, stderr_r):
        kernel.SetHandleInformation(handle, 0x1, 0)
    for handle in (stdin_r, stdout_w, stderr_w):
        kernel.SetHandleInformation(handle, 0x1, 0x1)
    attributes = None
    listed = False
    process = None
    thread = None
    job = None
    try:
        size = ctypes.c_size_t(0)
        kernel.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
        if not size.value:
            raise _win_fail("InitializeProcThreadAttributeList did not return a size")
        attributes = ctypes.create_string_buffer(size.value)
        if not kernel.InitializeProcThreadAttributeList(attributes, 2, 0, ctypes.byref(size)):
            raise _win_fail("InitializeProcThreadAttributeList failed")
        listed = True
        caps = _WinSecurityCapabilities(lease.sid, None, 0, 0)
        handles = (ctypes.c_void_p * 3)(stdin_r, stdout_w, stderr_w)
        if not kernel.UpdateProcThreadAttribute(
            attributes, 0, 0x20009, ctypes.byref(caps), ctypes.sizeof(caps), None, None
        ):
            raise _win_fail("the AppContainer attribute was not applied")
        if not kernel.UpdateProcThreadAttribute(
            attributes, 0, 0x20002, handles, ctypes.sizeof(handles), None, None
        ):
            raise _win_fail("the inherited-handle list was not applied")
        info = _WinStartupInfoEx()
        info.StartupInfo.cb = ctypes.sizeof(info)
        info.StartupInfo.dwFlags = 0x100
        info.StartupInfo.hStdInput = stdin_r
        info.StartupInfo.hStdOutput = stdout_w
        info.StartupInfo.hStdError = stderr_w
        info.lpAttributeList = ctypes.cast(attributes, ctypes.c_void_p)
        command = subprocess.list2cmdline(_interpreter_command(work / "sandbox_child.py", child=True))
        command_buf = ctypes.create_unicode_buffer(command)
        env_buf = _windows_env_block(_windows_env(work))
        created = _WinProcessInformation()
        # CREATE_SUSPENDED | EXTENDED_STARTUPINFO_PRESENT | CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT
        flags = 0x00000004 | 0x00080000 | 0x08000000 | 0x00000400
        ok = kernel.CreateProcessW(
            sys.executable,
            command_buf,
            None,
            None,
            1,
            flags,
            ctypes.cast(env_buf, ctypes.c_void_p),
            str(work),
            ctypes.byref(info),
            ctypes.byref(created),
        )
        if not ok:
            raise _win_fail("CreateProcessW failed")
        process = created.hProcess
        thread = created.hThread
        _win_require_container(lease, process)
        job = _win_arm_job(kernel, process, memory_bytes=memory_bytes, cpu_seconds=cpu_seconds)
        if kernel.ResumeThread(thread) == 0xFFFFFFFF:
            raise _win_fail("ResumeThread failed")
        kernel.CloseHandle(thread)
        thread = None
        for handle in (stdin_r, stdout_w, stderr_w):
            kernel.CloseHandle(handle)
        stdin_r = stdout_w = stderr_w = None
        binary = os.O_BINARY if hasattr(os, "O_BINARY") else 0
        stdin_fd = msvcrt.open_osfhandle(stdin_w, os.O_WRONLY | binary)
        stdout_fd = msvcrt.open_osfhandle(stdout_r, os.O_RDONLY | binary)
        stderr_fd = msvcrt.open_osfhandle(stderr_r, os.O_RDONLY | binary)
        stdin_w = stdout_r = stderr_r = None
        return _WindowsPopen(process, int(created.dwProcessId), stdin_fd, stdout_fd, stderr_fd, kernel, job)
    except Exception:
        if thread:
            kernel.CloseHandle(thread)
        if process:
            kernel.TerminateProcess(process, 1)
            kernel.CloseHandle(process)
        if job:
            kernel.TerminateJobObject(job, 1)
            kernel.CloseHandle(job)
        for handle in (stdin_r, stdin_w, stdout_r, stdout_w, stderr_r, stderr_w):
            if handle:
                kernel.CloseHandle(handle)
        raise
    finally:
        if listed:
            kernel.DeleteProcThreadAttributeList(attributes)


def _win_pipe(kernel):
    read = ctypes.c_void_p()
    write = ctypes.c_void_p()
    security = _WinSecurityAttributes(ctypes.sizeof(_WinSecurityAttributes), None, 1)
    # The default pipe is a few kilobytes. One bar is one write; a larger
    # buffer keeps that write from blocking on the child's first read.
    if not kernel.CreatePipe(ctypes.byref(read), ctypes.byref(write), ctypes.byref(security), 65536):
        raise _win_fail("CreatePipe failed")
    return read.value, write.value


def _win_require_container(lease: _WinLease, process) -> None:
    """Refuse to resume a process that is not an empty AppContainer."""
    advapi = lease.advapi
    kernel = lease.kernel
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
    token = ctypes.c_void_p()
    if not advapi.OpenProcessToken(process, 0x0008, ctypes.byref(token)):
        raise _win_fail("OpenProcessToken failed")
    try:
        flag = ctypes.c_uint32()
        needed = ctypes.c_uint32()
        if not advapi.GetTokenInformation(token, 29, ctypes.byref(flag), 4, ctypes.byref(needed)):
            raise _win_fail("TokenIsAppContainer failed")
        if int(flag.value) != 1:
            raise SandboxError(
                f"the child is not an AppContainer (TokenIsAppContainer={int(flag.value)}); refusing to resume"
            )
        advapi.GetTokenInformation(token, 30, None, 0, ctypes.byref(needed))
        size = int(needed.value or 4)
        buf = ctypes.create_string_buffer(size)
        if not advapi.GetTokenInformation(token, 30, buf, size, ctypes.byref(needed)):
            raise _win_fail("TokenCapabilities failed")
        count = int(ctypes.c_uint32.from_buffer_copy(buf).value)
        if count != 0:
            raise SandboxError(f"the AppContainer has {count} capabilities; refusing to resume")
    finally:
        kernel.CloseHandle(token)


def _local_app_data() -> Path | None:
    raw = os.environ.get("LOCALAPPDATA")
    if not raw:
        return None
    return Path(raw)


def _windows_env(work: Path) -> dict:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise SandboxError("LOCALAPPDATA is not set; refusing to start the sandbox")
    system = os.environ.get("SystemRoot", "")
    parts = [os.path.dirname(sys.executable)]
    if system:
        parts.append(str(Path(system) / "System32"))
        parts.append(system)
    env = {
        "PATH": os.pathsep.join(parts),
        "SYSTEMROOT": system,
        "WINDIR": system,
        "COMSPEC": os.environ.get("COMSPEC", ""),
        "PATHEXT": os.environ.get("PATHEXT", ".EXE;.PY"),
        "TEMP": str(work),
        "TMP": str(work),
        "HOME": str(work),
        "USERPROFILE": str(work),
        # The real directory. Pointing this at the private temp does not
        # locate the AppContainer profile, and CreateProcess then fails
        # with ERROR_ENVVAR_NOT_FOUND (203).
        "LOCALAPPDATA": local,
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    return {key: value for key, value in env.items() if value}


def _windows_env_entries(env: dict) -> list[str]:
    """``name=value`` lines in the order CreateProcess requires.

    Windows rejects an unsorted block with ERROR_ENVVAR_NOT_FOUND (203).
    Names are compared case-insensitively. A name may not contain ``=``.
    """
    entries = []
    for key in sorted(env, key=str.upper):
        if not key or "=" in key or "\0" in key:
            raise SandboxError(f"environment name {key!r} cannot be passed to CreateProcess")
        value = str(env[key])
        if not value or "\0" in value:
            continue
        entries.append(f"{key}={value}")
    if not entries:
        raise SandboxError("the sandbox environment is empty; refusing to start")
    return entries


def _windows_env_block(env: dict):
    """A double-null-terminated Unicode environment block.

    ``create_unicode_buffer`` stores a Python string through a setter that
    stops at the first null, so the block is filled one character at a time.
    """
    entries = _windows_env_entries(env)
    chars: list[str] = []
    for entry in entries:
        chars.extend(entry)
        chars.append("\0")
    chars.append("\0")
    buf = (ctypes.c_wchar * len(chars))()
    for index, char in enumerate(chars):
        buf[index] = char
    return buf


def _win_arm_job(kernel, process, *, memory_bytes: int, cpu_seconds: int):
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise _win_fail("CreateJobObjectW failed")
    info = _WinJobExtended()
    ticks = int(cpu_seconds) * 10_000_000
    info.BasicLimitInformation.PerProcessUserTimeLimit = ticks
    info.BasicLimitInformation.PerJobUserTimeLimit = ticks
    info.BasicLimitInformation.ActiveProcessLimit = 1
    info.BasicLimitInformation.LimitFlags = (
        0x00000008 | 0x00000002 | 0x00000004 | 0x00002000 | 0x00000100 | 0x00000200
    )
    info.ProcessMemoryLimit = int(memory_bytes)
    info.JobMemoryLimit = int(memory_bytes)
    if not kernel.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
        err = _win_fail("SetInformationJobObject failed")
        kernel.CloseHandle(job)
        raise err
    if not kernel.AssignProcessToJobObject(job, process):
        err = _win_fail("AssignProcessToJobObject failed")
        kernel.CloseHandle(job)
        raise err
    return job


class _WindowsPopen:
    """The slice of subprocess.Popen the referee uses, backed by CreateProcess."""

    def __init__(self, process, pid, stdin_fd, stdout_fd, stderr_fd, kernel, job):
        self._process = process
        self.pid = pid
        self.returncode = None
        self.stdin = _Fd(stdin_fd)
        self.stdout = _Fd(stdout_fd)
        self.stderr = _Fd(stderr_fd)
        self._kernel = kernel
        self._job = job

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        code = ctypes.c_uint32()
        if not self._kernel.GetExitCodeProcess(self._process, ctypes.byref(code)):
            return None
        if code.value == 259:
            return None
        self.returncode = int(code.value)
        return self.returncode

    def wait(self, timeout=None):
        millis = 0xFFFFFFFF if timeout is None else max(0, int(timeout * 1000))
        waited = self._kernel.WaitForSingleObject(self._process, millis)
        if waited != 0:
            raise subprocess.TimeoutExpired([sys.executable], timeout)
        return self.poll()

    def kill(self):
        if self._job:
            self._kernel.TerminateJobObject(self._job, 1)
        else:
            self._kernel.TerminateProcess(self._process, 1)


class _Fd:
    def __init__(self, fd: int):
        self._fd = fd

    def fileno(self) -> int:
        return self._fd

    def close(self) -> None:
        if self._fd is None:
            return
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None
