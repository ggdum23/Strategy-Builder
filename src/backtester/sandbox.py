"""OS jail for the referee child.

Windows: a job object (kill on close, one active process, a memory cap, a CPU
cap) and an AppContainer with no network capability. File ACLs are edited
only on directories this app owns or creates. The private temp directory is
the only writable location. A directory that was not granted is not readable,
because the token is not the user. If any of that cannot be applied, the
launch raises and the strategy is not started.

Linux: bubblewrap when ``bwrap`` can start a throwaway namespace. Otherwise
landlock plus a seccomp filter, rlimits, and ``PR_SET_PDEATHSIG``. The report
from ``describe_isolation`` is the guarantee that was actually applied.

``lock_worker_session`` is the smaller filter used by the walk-forward and
parallel workers after they become session leaders. It denies ``setsid`` and
``setpgid`` so a grandchild cannot leave the process group.
"""

from __future__ import annotations

import atexit
import ctypes
import hashlib
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


# GitHub-hosted AppContainer startup is about 16–20 seconds before the
# child writes a ready line. The Windows hello wait is this long so that
# cost is not the strategy's call timeout. Tests use the same number.
WINDOWS_JAIL_STARTUP_S = 90

# The pytest process registers this. A spawned worker does not, so a jail
# the worker starts is counted by the parent that decided to start it.
_LAUNCH_OBSERVER = None
_PHASE_PATH = None
_PHASE_LOCK = threading.Lock()
_LOCK_DEPTH = threading.local()


def set_launch_observer(observer) -> None:
    """Count successful jails. The observer runs in this process only."""
    global _LAUNCH_OBSERVER
    _LAUNCH_OBSERVER = observer


def note_launch() -> None:
    observer = _LAUNCH_OBSERVER
    if observer is not None:
        observer()


def watch_phase(path) -> None:
    """Append phase lines to this file. The parent reads them on a hang."""
    global _PHASE_PATH
    _PHASE_PATH = None if path is None else str(path)


def note_phase(name: str) -> None:
    path = _PHASE_PATH
    if not path:
        return
    line = f"{time.monotonic():.3f} {name}\n"
    with _PHASE_LOCK:
        try:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line)
        except OSError:
            pass


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
_ENOSYS = 38
_AUDIT_ARCH_X86_64 = 0xC000003E
_PR_SET_PDEATHSIG = 1
_PR_SET_NO_NEW_PRIVS = 38
_PR_SET_SECCOMP = 22
_SECCOMP_MODE_FILTER = 2
_CLONE_THREAD = 0x00010000
# x32 reuses the x86_64 audit architecture and sets this bit on the number.
# A denylist of native numbers does not match those calls, so they would
# fall through to ALLOW. The filter kills them.
_X32_SYSCALL_BIT = 0x40000000

# x86_64 syscall numbers. Other machines fail closed rather than guess.
# aarch64 uses the generic table for io_uring_setup, io_uring_enter, and
# io_uring_register (425, 426, 427). Its other numbers differ, so there is
# no aarch64 map: an unknown machine refuses the launch instead of guessing.
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
    "io_uring_setup": 425,
    "io_uring_enter": 426,
    "io_uring_register": 427,
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
    "io_uring_setup",
    "io_uring_enter",
    "io_uring_register",
    "pidfd_getfd",
)

_LANDLOCK_HANDLED_FS = 0xFFFF
_LANDLOCK_EXECUTE = 1 << 0
_LANDLOCK_READ_FILE = 1 << 2
_LANDLOCK_READ = _LANDLOCK_EXECUTE | _LANDLOCK_READ_FILE | (1 << 3)  # execute, read file, read dir
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
        # Kill x32 numbers before the native compare. AND clears the
        # accumulator, so the native number is loaded again afterwards.
        _insn(_ALU_AND, 0, 0, _X32_SYSCALL_BIT),
        _insn(_JMP_JEQ, 0, 1, _X32_SYSCALL_BIT),
        _insn(_RET_K, 0, 0, _SECCOMP_RET_KILL),
        _insn(_LD_W_ABS, 0, 0, 0),
    ]
    for name in deny:
        program.append(_insn(_JMP_JEQ, 0, 1, numbers[name]))
        program.append(_insn(_RET_K, 0, 0, _SECCOMP_RET_ERRNO | _EPERM))
    if allow_threads:
        # clone is allowed only when CLONE_THREAD is set, so a thread can
        # start and a process cannot. clone3's flag pointer is not visible to
        # classic BPF, so the whole call returns ENOSYS and glibc falls back
        # to clone. A fork bomb cannot use clone3 to skip that check.
        program.extend(
            (
                _insn(_JMP_JEQ, 0, 1, numbers["clone3"]),
                _insn(_RET_K, 0, 0, _SECCOMP_RET_ERRNO | _ENOSYS),
                _insn(_JMP_JEQ, 0, 4, numbers["clone"]),
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
            "the rest of the socket calls, and io_uring_setup, io_uring_enter, "
            "and io_uring_register"
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
            "fork, vfork, and clone without CLONE_THREAD. clone3 returns ENOSYS so "
            "glibc creates threads with clone. Classic BPF cannot read clone3's flag "
            "pointer, and a process started that way would not be stopped by the "
            "CLONE_THREAD check."
        ),
        "parent_memory": (
            "the host /proc is not mounted. ptrace and process_vm_readv return EPERM"
        ),
        "memory": (
            "RLIMIT_AS, RLIMIT_DATA, and RLIMIT_NPROC hard caps, set before the "
            "strategy is loaded. The process ceiling is 64 inside the user namespace."
        ),
        "cpu": "RLIMIT_CPU hard cap, plus the parent's wall-clock per-call timeout",
        "fail_closed": True,
    }


def _landlock_guarantees() -> dict:
    return {
        "os": "linux",
        "backend": "landlock+seccomp",
        "network": (
            "seccomp returns EPERM for socket, connect, and io_uring_setup, "
            "io_uring_enter, and io_uring_register. A network namespace is not created."
        ),
        "filesystem": (
            "landlock allow-list: read the Python runtime and the staged packages, "
            "traverse /etc, and read only /etc/ld.so.cache. The rest of /etc is not "
            "readable. stat is not mediated by landlock and is not a confinement "
            "check. Opening a data or repository path returns EPERM. This is not a "
            "mount namespace. The child is python -I -S, so site-packages is not on "
            "sys.path. Landlock rules are directories, so a prefix that contains "
            "site-packages cannot have that one directory punched out."
        ),
        "process": (
            "seccomp denies setsid, setpgid, fork, vfork, and clone without CLONE_THREAD. "
            "clone3 returns ENOSYS, the same as bubblewrap, so a fork bomb cannot skip "
            "the CLONE_THREAD check. PR_SET_PDEATHSIG is SIGKILL. There is no pid "
            "namespace. bubblewrap is the stronger backend and is used when it runs."
        ),
        "parent_memory": "/proc is not in the allow-list, so /proc/<pid>/mem cannot be opened. ptrace is denied.",
        "memory": (
            "RLIMIT_AS, RLIMIT_DATA, and RLIMIT_NPROC hard caps. The process ceiling "
            "is the uid's current task count plus 64, counted before the jail starts, "
            "because this backend has no user namespace."
        ),
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
            "access. ACLs are edited only on directories this app owns: the private "
            "temp directory, an app-data directory it created, and the interpreter "
            "when that copy is outside Windows, Program Files, the drive root, and "
            "the user-profile root. System directories are not edited. Ancestors "
            "this app does not own are not rewritten. An interpreter whose "
            "ancestors lack traverse is copied under an app-created directory "
            "that the container can already reach, and only the copy is granted. "
            "If no such directory exists, the launch fails and names the ancestor. "
            "Data, market, and repository trees are not granted. "
            "LOCALAPPDATA is the host value the profile needs, and its DACL is not "
            "edited. A child that is not an AppContainer is not resumed."
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
            try:
                from .limits import release_cgroup_memory

                release_cgroup_memory(int(self.process.pid))
            except (OSError, ValueError, ImportError):
                pass
            shutil.rmtree(self.workdir, ignore_errors=True)


def _interpreter_command(script: Path, *, child: bool = False) -> list[str]:
    """How to execute a jail script with this interpreter.

    A source child is ``python -I -S -B``. ``-S`` skips site.py, so the
    editable install and dist-packages are not added to ``sys.path``. The
    parent then inserts only the staged package directories. A frozen child
    is this executable again, with ``--sandbox-child`` and the private
    directory as its cwd. No strategy path is put on that command line.
    """
    executable = _child_executable()
    if child and getattr(sys, "frozen", False):
        return [executable, "--sandbox-child"]
    if getattr(sys, "frozen", False):
        return [executable, str(script)]
    if child:
        return [executable, "-I", "-S", "-B", str(script)]
    return [executable, "-I", "-S", "-B", str(script)]


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
    # The workspace junctions have to point at the staged interpreter. On
    # Windows that copy is decided while the profile is granted, which is
    # before the private directory is written.
    warm_windows_profile()
    strategy = Path(strategy_path).resolve()
    if not strategy.is_file():
        raise FileNotFoundError(f"strategy file not found: {strategy}")
    denied = _deny_roots(deny_paths)
    work = Path(_private_dir())
    try:
        for banned in denied:
            if work == banned or banned in work.parents or work in banned.parents:
                raise SandboxError("the private directory overlaps a denied directory")
        if sys.platform == "win32":
            backend = "windows"
        elif sys.platform == "linux" and _bwrap_available():
            backend = "bubblewrap"
        elif sys.platform == "linux" and _landlock_available():
            backend = "landlock"
        else:
            backend = None
        _write_workspace(
            work,
            strategy,
            memory_bytes=memory_bytes,
            cpu_seconds=cpu_seconds,
            capture_stdio=capture_stdio,
            nproc=_nproc_ceiling(backend),
        )
        if backend == "windows":
            box = _launch_windows(work, denied, memory_bytes=memory_bytes, cpu_seconds=cpu_seconds)
        elif sys.platform != "linux":
            raise SandboxError(f"no referee jail for {sys.platform}; refusing to run the strategy")
        elif backend == "bubblewrap":
            box = _launch_bwrap(work, denied, memory_bytes=memory_bytes, cpu_seconds=cpu_seconds)
        elif backend == "landlock":
            box = _launch_landlock(work, denied, memory_bytes=memory_bytes, cpu_seconds=cpu_seconds)
        else:
            raise SandboxError(
                "bubblewrap is not available and landlock cannot be applied; refusing to run the strategy"
            )
        if sys.platform == "linux":
            from .limits import apply_cgroup_memory_max

            apply_cgroup_memory_max(int(box.process.pid), int(memory_bytes))
        note_launch()
        return box
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise


# Directories this process created for the jail (app data, not the user profile).
_CREATED_APP_DIRS: list[Path] = []


def _private_dir() -> str:
    """A directory the container can already reach.

    The default user Temp directory is not that parent on the GitHub
    runner. The runtime copy's parent is: ProgramData or Public when
    those already have traverse, otherwise the AppContainer folder.
    """
    import tempfile

    parent = _owned_work_parent()
    if parent is None and _RUNTIME_PARENT:
        parent = Path(_RUNTIME_PARENT)
    if parent is None:
        return tempfile.mkdtemp(prefix="bt-referee-")
    return tempfile.mkdtemp(prefix="bt-referee-", dir=str(parent))


def _chain_accessible(path, accessible) -> bool:
    """True when ``path`` and every non-root ancestor already pass ``accessible``.

    This does not write an ACL. The volume root is not required to show an
    explicit ACE and is never a grant target.
    """
    if _is_volume_acl_path(path) or not accessible(path):
        return False
    return all(
        accessible(higher)
        for higher in acl_ancestor_paths(path)
        if not _is_volume_acl_path(higher)
    )


def copy_parent_candidates(environ, roots, accessible) -> list[str]:
    """Directories an app-owned runtime copy may be created under.

    ProgramData and Public come first, when that directory and every
    ancestor already let the container through. Otherwise each blocked
    runtime contributes its traversable ancestors, nearest first. The
    blocked directory, the volume root, and anything inside Windows or
    System32 are not candidates. Program Files is last, and only as a
    parent: the directory itself is not a grant target.
    """
    found: list[str] = []
    _exact, trees = protected_acl_layout(environ)

    def add(raw) -> None:
        if raw is None or raw == "" or _is_volume_acl_path(raw):
            return
        if any(acl_same(raw, tree) or _acl_inside(raw, tree) for tree in trees):
            return
        if any(acl_same(raw, item) for item in found):
            return
        if not _chain_accessible(raw, accessible):
            return
        found.append(str(raw))

    for key in ("ProgramData", "PUBLIC"):
        add(environ.get(key, ""))
    for root in roots:
        if not root:
            continue
        blocked = first_blocked_ancestor(str(root), [], accessible)
        if blocked is None:
            continue
        for parent in acl_ancestor_paths(blocked):
            add(parent)
    for key in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        add(environ.get(key, ""))
    return found


def _read_dacl_entries(path: Path):
    """``(type, flags, mask)`` for every ACE, or None when the DACL cannot be read."""
    if sys.platform != "win32":
        return []
    try:
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    except OSError:
        return None
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
    advapi.GetAce.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
    advapi.GetAce.restype = ctypes.c_int
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    dacl = ctypes.c_void_p()
    security = ctypes.c_void_p()
    status = advapi.GetNamedSecurityInfoW(
        str(path), 1, 4, None, None, ctypes.byref(dacl), None, ctypes.byref(security)
    )
    if status != 0 or not security.value:
        if security.value:
            kernel.LocalFree(security)
        return None
    try:
        if not dacl.value:
            return []
        header = _ACL_HEADER.from_address(int(dacl.value))
        found = []
        for index in range(min(int(header.AceCount), 256)):
            ace = ctypes.c_void_p()
            if not advapi.GetAce(dacl, index, ctypes.byref(ace)) or not ace.value:
                continue
            info = _ACE_HEADER.from_address(int(ace.value))
            mask = int(ctypes.c_uint32.from_address(int(ace.value) + 4).value)
            found.append((int(info.AceType), int(info.AceFlags), mask))
        return found
    finally:
        kernel.LocalFree(security)


def _parent_inheritable_write(path: Path) -> bool:
    """Fail closed when the DACL cannot be read."""
    entries = _read_dacl_entries(path)
    if entries is None:
        return True
    return dacl_has_inheritable_write(entries)


def _sid_text(advapi, kernel, sid) -> str:
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
    advapi.ConvertSidToStringSidW.restype = ctypes.c_int
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    text = ctypes.c_wchar_p()
    if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)) or not text.value:
        return ""
    try:
        return str(text.value)
    finally:
        kernel.LocalFree(text)


def _current_user_sid_text() -> str:
    if sys.platform != "win32":
        return ""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    kernel.OpenProcessToken.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
    kernel.OpenProcessToken.restype = ctypes.c_int
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    advapi.GetTokenInformation.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    advapi.GetTokenInformation.restype = ctypes.c_int
    token = ctypes.c_void_p()
    if not kernel.OpenProcessToken(kernel.GetCurrentProcess(), 0x8, ctypes.byref(token)):
        return ""
    try:
        needed = ctypes.c_uint32()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        buf = ctypes.create_string_buffer(max(int(needed.value), 8))
        if not advapi.GetTokenInformation(token, 1, buf, len(buf), ctypes.byref(needed)):
            return ""
        sid = ctypes.c_void_p.from_buffer(buf)
        return _sid_text(advapi, kernel, sid)
    finally:
        kernel.CloseHandle(token)


def _owner_sid_text(path: Path) -> str:
    if sys.platform != "win32":
        return ""
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.GetNamedSecurityInfoW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi.GetNamedSecurityInfoW.restype = ctypes.c_uint32
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    owner = ctypes.c_void_p()
    security = ctypes.c_void_p()
    # OWNER_SECURITY_INFORMATION = 1
    status = advapi.GetNamedSecurityInfoW(
        str(path), 1, 1, ctypes.byref(owner), None, None, None, ctypes.byref(security)
    )
    if status != 0 or not security.value or not owner.value:
        if security.value:
            kernel.LocalFree(security)
        return ""
    try:
        return _sid_text(advapi, kernel, owner)
    finally:
        kernel.LocalFree(security)


def _directory_owned_by_us(path: Path) -> bool:
    return owner_matches(_owner_sid_text(path), _current_user_sid_text())


def _owned_work_parent() -> Path | None:
    """An app-created folder whose parents already allow package traverse.

    The DACL of the parent is not modified. This is the fallback used when
    the per-user container folder cannot be created. ProgramData and Public
    are tried only when they already let the package through, have no
    inheritable write ACE, and an existing child is owned by this user.
    The volume root, Windows, and System32 are never used. Only the
    directories this function creates are later granted.
    """
    if sys.platform != "win32":
        return None
    roots = [str(path) for path in _interpreter_candidates()]
    for raw in copy_parent_candidates(os.environ, roots, _ancestor_accessible):
        parent = Path(raw)
        try:
            if not parent.is_dir():
                continue
            app = (parent / "Strategy Builder").resolve()
            jail = (app / "jail").resolve()
            if not _can_be_allow_root(app, os.environ) or not _can_be_allow_root(jail, os.environ):
                continue
            created_app = not app.exists()
            if not shared_parent_usable(
                traversable=_chain_has_package_traverse(parent),
                inheritable_write=_parent_inheritable_write(parent),
                created=created_app,
                owner_ok=True if created_app else _directory_owned_by_us(app),
            ):
                continue
            app.mkdir(exist_ok=True)
            _reject_reparse_dir(app)
            jail.mkdir(exist_ok=True)
            _reject_reparse_dir(jail)
            # The directory we would hand back, not the parent we checked
            # before mkdir. An adopted directory has to be ours, and its
            # DACL must not still hand write to a new child.
            if _parent_inheritable_write(jail) or not _directory_owned_by_us(jail):
                continue
            if not _directory_owned_by_us(app):
                continue
        except (OSError, SandboxError):
            continue
        for directory in (app, jail):
            if not any(acl_same(directory, existing) for existing in _CREATED_APP_DIRS):
                _CREATED_APP_DIRS.append(directory)
        return jail
    return None


def _chain_has_package_traverse(path: Path) -> bool:
    """Read-only. The volume root is not required to show an explicit ACE."""
    return _chain_accessible(path, _ancestor_accessible)


def _interpreter_venv() -> Path | None:
    """The virtualenv prefix, when this interpreter is one."""
    prefix = Path(sys.prefix).resolve()
    base = Path(getattr(sys, "base_prefix", sys.prefix)).resolve()
    if prefix == base:
        return None
    if not (prefix / "pyvenv.cfg").is_file():
        return None
    return prefix


def runtime_overlap_error(root, denied, *, venv=None) -> str | None:
    """None when ``root`` may be mounted. A sentence when it must be refused.

    ``venv`` is the virtualenv prefix. A prefix strictly inside ``denied`` is
    mounted on its own and does not expose the rest of that directory. A
    prefix that is the denied directory would expose the whole tree, so that
    mount is refused.
    """
    root = Path(root)
    denied = Path(denied)
    if root != denied and denied not in root.parents:
        return None
    if venv is not None:
        prefix = Path(venv)
        under_prefix = root == prefix or prefix in root.parents
        if under_prefix and denied in prefix.parents:
            return None
        if under_prefix and prefix == denied:
            return (
                "the Python runtime is the denied directory itself. "
                "Create a virtualenv inside the clone with python -m venv .venv, "
                "or use an interpreter outside the clone."
            )
    return f"the Python runtime {root} is inside a denied directory; refusing to mount it"


def _deny_roots(paths) -> list[Path]:
    roots = []
    raw = list(paths or [])
    # A source tree sits outside the interpreter, so the child must not read
    # it. A frozen bundle is the interpreter: ``parents[2]`` is the install
    # directory, and denying it overlaps the runtime grant.
    if not getattr(sys, "frozen", False):
        raw.append(Path(__file__).resolve().parents[2])
    try:
        from .prepared import default_cache_dir

        raw.append(default_cache_dir())
    except Exception:
        pass
    for item in raw:
        if not item:
            continue
        path = Path(item).resolve()
        if path not in roots:
            roots.append(path)
    return roots


# Tasks a jail may create beyond the ones that already exist. Bubblewrap
# counts only its user namespace, so 64 is the whole jail. Landlock counts
# the host uid, so the same 64 is headroom on top of that count.
_NPROC_HEADROOM = 64


def _uid_task_count() -> int:
    """Threads on this uid. RLIMIT_NPROC counts tasks, not thread groups.

    Landlock does not hide ``/proc``, but the child cannot read it after the
    ruleset is applied. The parent counts first and writes the ceiling down.
    """
    uid = os.getuid()
    total = 0
    proc = Path("/proc")
    try:
        names = list(proc.iterdir())
    except OSError as exc:
        raise SandboxError(f"cannot count tasks for the process ceiling: {exc}") from exc
    for entry in names:
        if not entry.name.isdigit():
            continue
        try:
            text = (entry / "status").read_text(encoding="utf-8")
        except OSError:
            continue
        real = None
        threads = 0
        for line in text.splitlines():
            if line.startswith("Uid:"):
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        real = int(parts[1])
                    except ValueError:
                        real = None
            elif line.startswith("Threads:"):
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        threads = int(parts[1])
                    except ValueError:
                        threads = 0
        if real == uid:
            total += threads
    if total < 1:
        raise SandboxError("cannot count tasks for the process ceiling")
    return total


def _nproc_ceiling(backend: str | None) -> int | None:
    """The RLIMIT_NPROC hard cap for this backend, or None off Linux."""
    if backend == "bubblewrap":
        return _NPROC_HEADROOM
    if backend == "landlock":
        return _uid_task_count() + _NPROC_HEADROOM
    return None


def _write_workspace(
    work: Path,
    strategy: Path,
    *,
    memory_bytes: int,
    cpu_seconds: int,
    capture_stdio: bool,
    nproc: int | None = None,
) -> None:
    package = Path(__file__).resolve().parent
    shim = package / "sandbox_child.py"
    ipc = package / "bar_ipc.py"
    (work / "strategy.py").write_bytes(strategy.read_bytes())
    # A frozen jail is the executable itself. There is no system python and
    # no .py shim on disk. Source runs still copy the shim and the frame codec.
    if not getattr(sys, "frozen", False):
        if not shim.is_file():
            raise SandboxError("sandbox_child.py is missing")
        if not ipc.is_file():
            raise SandboxError("bar_ipc.py is missing")
        (work / "sandbox_child.py").write_bytes(shim.read_bytes())
        (work / "bar_ipc.py").write_bytes(ipc.read_bytes())
    limits = {
        "memory_bytes": int(memory_bytes),
        "cpu_seconds": int(cpu_seconds),
        "capture_stdio": bool(capture_stdio),
    }
    if nproc is not None:
        limits["nproc"] = int(nproc)
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
    venv = _interpreter_venv()
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
            inside_venv = venv is not None and (resolved == venv or venv in resolved.parents)
            # The checkout itself stays unstaged. Packages that live in a
            # virtualenv inside the checkout are the interpreter, and the
            # jail mounts that prefix alone.
            if not inside_venv or resolved.name == "backtester":
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
    ``*.libs`` directory is not linked into the slot. On Windows the child
    adds the real directory with ``os.add_dll_directory``; a junction there
    makes that call fail.
    """
    packages = _runtime_package_dirs()
    stage = work / "pydeps"
    stage.mkdir()
    entries = []
    dlls = []
    slots: dict[Path, Path] = {}
    index = 0
    for path in packages:
        source = _translate_runtime(path)
        if path.name.endswith(".libs"):
            owner = path.parent / path.name[: -len(".libs")]
            if slots.get(owner) is not None:
                # Not linked into the slot. os.add_dll_directory rejects that
                # junction on Windows, and the loader raises before the import.
                # The path is the staged copy when the original prefix cannot
                # be traversed. A junction back to that prefix would not open.
                dlls.append(str(source))
                continue
        slot = stage / f"{index:02d}"
        slot.mkdir()
        _link_into(source, slot / path.name)
        slots[path] = slot
        entries.append(f"pydeps/{index:02d}")
        index += 1
        if path.name.endswith(".libs"):
            dlls.append(str(source))
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
        # One BLAS thread. The process ceiling still has to leave room for a
        # strategy thread; this only stops the default pool from taking it.
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
    }
    return env


def _under_tmp(path: Path) -> bool:
    """True when ``path`` is ``/tmp`` or a directory inside it."""
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    try:
        tmp = Path("/tmp").resolve()
    except OSError:
        tmp = Path("/tmp")
    return resolved == tmp or tmp in resolved.parents


def bwrap_mount_plan(roots, packages, hidden_sites, work, cache_file) -> list[str]:
    """Mount arguments. A runtime root under ``/tmp`` is bound after the tmpfs.

    ``/tmp`` itself is not a bind target: the private tmpfs would hide it,
    and binding it back would undo the tmpfs. That launch is refused by
    the caller. The cache file is the only bind from ``/etc``.
    """
    early = []
    late = []
    for root in roots:
        path = Path(root)
        if path == Path("/tmp") or _under_tmp(path) and path.resolve() == Path("/tmp").resolve():
            raise SandboxError(f"refusing to hide the runtime at {path} under a private tmpfs")
        if _under_tmp(path):
            late.append(path)
        else:
            early.append(path)
    cmd: list[str] = []
    for root in early:
        cmd.extend(["--ro-bind", str(root), str(root)])
    for directory in hidden_sites:
        cmd.extend(["--tmpfs", str(directory)])
    for path in packages:
        package = Path(path)
        if _under_tmp(package):
            late.append(package)
        else:
            cmd.extend(["--ro-bind", str(package), str(package)])
    if cache_file:
        cmd.extend(["--dir", "/etc", "--ro-bind", str(cache_file), str(cache_file)])
    cmd.extend(["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"])
    for root in late:
        cmd.extend(["--ro-bind", str(root), str(root)])
    cmd.extend(["--bind", str(work), str(work)])
    return cmd


def _launch_bwrap(work: Path, denied: list[Path], *, memory_bytes: int, cpu_seconds: int) -> Sandbox:
    binary = _which("bwrap")
    if not binary:
        raise SandboxError("bubblewrap disappeared before launch")
    roots = _runtime_roots()
    venv = _interpreter_venv()
    for root in roots:
        for banned in denied:
            message = runtime_overlap_error(root, banned, venv=venv)
            if message is not None:
                raise SandboxError(message)
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
    cache = Path("/etc/ld.so.cache")
    cmd.extend(
        bwrap_mount_plan(
            roots,
            packages,
            hidden_sites,
            work,
            cache if cache.is_file() else None,
        )
    )
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


def landlock_rule_plan(read_dirs, cache_file, work) -> tuple[list, list]:
    """Directory rules and the one file rule for the dynamic linker cache.

    ``/etc`` is traverse-only. The cache file is a read-file rule. The
    directory that contains the cache is not granted read.
    """
    directories = []
    for directory in read_dirs:
        if not directory:
            continue
        path = str(directory)
        if path in ("/etc", "/etc/"):
            continue
        directories.append([path, _LANDLOCK_READ])
    files = []
    if cache_file:
        etc = str(Path(cache_file).parent)
        if etc and not any(item[0] == etc for item in directories):
            directories.append([etc, _LANDLOCK_EXECUTE])
        files.append([str(cache_file), _LANDLOCK_READ_FILE])
    directories.append([str(work), _LANDLOCK_WRITE])
    return directories, files


def _launch_landlock(work: Path, denied: list[Path], *, memory_bytes: int, cpu_seconds: int) -> Sandbox:
    """Landlock, seccomp, rlimits, and a parent-death signal. No preexec fork."""
    read_dirs = [str(path) for path in _runtime_roots()]
    for package in _runtime_package_dirs():
        directory = package if package.is_dir() else package.parent
        if str(directory) not in read_dirs:
            read_dirs.append(str(directory))
    cache = Path("/etc/ld.so.cache")
    venv = _interpreter_venv()
    for directory in read_dirs:
        path = Path(directory)
        for banned in denied:
            message = runtime_overlap_error(path, banned, venv=venv)
            if message is not None:
                raise SandboxError(message)
    rules, files = landlock_rule_plan(
        [directory for directory in read_dirs if directory and os.path.isdir(directory)],
        str(cache) if cache.is_file() else None,
        work,
    )
    (work / "filter.bin").write_bytes(_filter_bytes(deny=_SANDBOX_DENY, allow_threads=True))
    (work / "jail.json").write_text(
        json.dumps(
            {
                "handled": _LANDLOCK_HANDLED_FS,
                "rules": rules,
                "files": files,
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
        for filename, access in spec.get("files") or []:
            if not filename or not os.path.isfile(filename):
                continue
            raw = os.open(filename, os.O_PATH | os.O_CLOEXEC)
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
            # The AppContainer token is the file boundary. A directory that
            # was not granted is not readable, so the repository is not
            # given a deny ACE. The private directory is the only grant that
            # changes per launch. Ancestors this app does not own are not
            # rewritten; the launch fails if one of them lacks traverse.
            # The profile lock still serializes the grant and the spawn.
            def _grant_and_spawn():
                nonlocal extra_grants, popen
                try:
                    extra_grants = _win_grant_work(lease, work)
                    popen = _win_spawn(work, lease, memory_bytes=memory_bytes, cpu_seconds=cpu_seconds)
                except Exception:
                    # Drop the ACEs before the next waiter starts. A drop
                    # after the lock is released can remove the traverse ACE
                    # that waiter just inherited.
                    _win_drop_launch_aces(lease, [], [], extra_grants)
                    extra_grants = []
                    raise

            _with_profile_lock(_grant_and_spawn)
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
                _drop_launch_aces_locked(lease, extra_grants)
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
        self.acl_allow: list[str] = []
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

    The first jail in a process pays for the Win32 ACL walk of the
    interpreter. Later launches, including workers spawned after this
    returns, reuse the marker and only grant their private directory.
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


def _drop_launch_aces_locked(lease, extra_grants) -> None:
    """Drop the launch ACEs while this process holds the profile mutex."""
    _with_profile_lock(lambda: _win_drop_launch_aces(lease, [], [], extra_grants))


def _with_profile_lock(action):
    """One grant or spawn at a time. The kernel drops the mutex if the holder dies.

    A file lock in ``LOCALAPPDATA`` survived ``TerminateProcess`` for the
    stale window, and the next jail then sat on it until its own timeout.
    ``icacls`` on that same directory also blocked behind the open file.
    A named mutex is released on process exit, including an abandoned one
    left by a killed holder. Nested calls on this thread reuse it.
    """
    if sys.platform != "win32":
        return action()
    depth = getattr(_LOCK_DEPTH, "n", 0)
    if depth:
        return action()
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.WaitForSingleObject.restype = ctypes.c_uint32
    kernel.ReleaseMutex.argtypes = [ctypes.c_void_p]
    kernel.ReleaseMutex.restype = ctypes.c_int
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    handle = kernel.CreateMutexW(None, 0, "Local\\strategybuilderjail")
    if not handle:
        raise _win_fail("CreateMutexW failed")
    note_phase("lock-wait")
    # WAIT_OBJECT_0 is 0. WAIT_ABANDONED is 0x80: the previous holder died
    # and this thread now owns the mutex.
    result = kernel.WaitForSingleObject(handle, 300_000)
    if result not in (0, 0x80):
        kernel.CloseHandle(handle)
        raise SandboxError("timed out waiting for the AppContainer grant")
    note_phase("lock-held")
    _LOCK_DEPTH.n = 1
    try:
        return action()
    finally:
        _LOCK_DEPTH.n = 0
        note_phase("lock-released")
        kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)


def profile_lock_state() -> str:
    """``free``, ``held``, or ``abandoned``. Used on the Windows hang path."""
    if sys.platform != "win32":
        return "n/a"
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.WaitForSingleObject.restype = ctypes.c_uint32
    kernel.ReleaseMutex.argtypes = [ctypes.c_void_p]
    kernel.ReleaseMutex.restype = ctypes.c_int
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    handle = kernel.CreateMutexW(None, 0, "Local\\strategybuilderjail")
    if not handle:
        return f"mutex-error={ctypes.get_last_error()}"
    result = kernel.WaitForSingleObject(handle, 0)
    try:
        if result == 0:
            return "free"
        if result == 0x80:
            return "abandoned"
        if result == 0x102:
            return "held"
        return f"wait={result}"
    finally:
        if result in (0, 0x80):
            kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)


_WFP_ENGINE = None
_WFP_LAYERS = {"v4": False, "v6": False}


def wfp_connect_layers() -> tuple:
    """ALE auth-connect layer ids for IPv4 and IPv6. Both are required."""
    return (
        (0xC38D57D1, 0x05A7, 0x4C33, (0x90, 0x4F, 0x7F, 0xBC, 0xEE, 0xE6, 0x0E, 0x82)),
        (0x4A72393B, 0x319F, 0x44BC, (0x84, 0xC3, 0xBA, 0x54, 0xDC, 0xB3, 0xB6, 0xB4)),
    )


def firewall_timeout_blocks_launch(v4_ready: bool, v6_ready: bool) -> bool:
    """A hung firewall cmdlet fails the launch unless both WFP layers are in."""
    return not (bool(v4_ready) and bool(v6_ready))


def wfp_layers_confirmed() -> bool:
    return bool(_WFP_LAYERS.get("v4")) and bool(_WFP_LAYERS.get("v6"))


def _win_wfp_block(lease: _WinLease) -> None:
    """Block this package at the connect layer, including loopback.

    Windows drops an AppContainer's loopback handshake in the receive layer,
    so ``connect`` waits until it times out. A connect-layer block for the
    package SID fails that call with access denied before the handshake.
    The filter is dynamic and lives as long as this process holds the engine.
    """
    global _WFP_ENGINE
    if _WFP_ENGINE is not None:
        return

    class Guid(ctypes.Structure):
        _fields_ = (
            ("Data1", ctypes.c_uint32),
            ("Data2", ctypes.c_uint16),
            ("Data3", ctypes.c_uint16),
            ("Data4", ctypes.c_ubyte * 8),
        )

    class Value(ctypes.Structure):
        _fields_ = (("type", ctypes.c_uint32), ("ptr", ctypes.c_void_p))

    class Condition(ctypes.Structure):
        _fields_ = (("fieldKey", Guid), ("matchType", ctypes.c_uint32), ("value", Value))

    class Action(ctypes.Structure):
        _fields_ = (("type", ctypes.c_uint32), ("filterType", Guid))

    class Display(ctypes.Structure):
        _fields_ = (("name", ctypes.c_wchar_p), ("description", ctypes.c_wchar_p))

    class Blob(ctypes.Structure):
        _fields_ = (("size", ctypes.c_uint32), ("data", ctypes.c_void_p))

    class Filter(ctypes.Structure):
        _fields_ = (
            ("filterKey", Guid),
            ("displayData", Display),
            ("flags", ctypes.c_uint32),
            ("providerKey", ctypes.c_void_p),
            ("providerData", Blob),
            ("layerKey", Guid),
            ("subLayerKey", Guid),
            ("weight", Value),
            ("numFilterConditions", ctypes.c_uint32),
            ("filterCondition", ctypes.POINTER(Condition)),
            ("action", Action),
            ("rawContext", ctypes.c_uint64),
            ("reserved", ctypes.c_void_p),
            ("filterId", ctypes.c_uint64),
            ("effectiveWeight", Value),
        )

    def guid(data1, data2, data3, data4) -> Guid:
        item = Guid()
        item.Data1 = data1
        item.Data2 = data2
        item.Data3 = data3
        for index, byte in enumerate(data4):
            item.Data4[index] = byte
        return item

    fwpu = ctypes.WinDLL("fwpuclnt", use_last_error=True)
    class Session(ctypes.Structure):
        _fields_ = (
            ("sessionKey", Guid),
            ("displayData", Display),
            ("flags", ctypes.c_uint32),
            ("txnWaitTimeoutInMSec", ctypes.c_uint32),
            ("processId", ctypes.c_uint32),
            ("sid", ctypes.c_void_p),
            ("username", ctypes.c_wchar_p),
            ("kernelMode", ctypes.c_int),
        )

    fwpu.FwpmEngineOpen0.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(Session),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    fwpu.FwpmEngineOpen0.restype = ctypes.c_uint32
    fwpu.FwpmFilterAdd0.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(Filter),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint64),
    ]
    fwpu.FwpmFilterAdd0.restype = ctypes.c_uint32
    engine = ctypes.c_void_p()
    name = "strategybuilderjail block connect"
    # FWPM_SESSION_FLAG_DYNAMIC. A null session is static: the filter stays
    # installed after this process exits, and every worker adds another one.
    # A dynamic session removes the filter when the process exits. The handle
    # is kept below so the filter lasts for every jail this process starts.
    session = Session()
    session.displayData.name = name
    session.displayData.description = name
    session.flags = 0x1
    # RPC_C_AUTHN_WINNT
    status = fwpu.FwpmEngineOpen0(None, 10, None, ctypes.byref(session), ctypes.byref(engine))
    if status != 0 or not engine.value:
        raise SandboxError(f"the firewall engine did not open (winerror {status})")
    # Highest weight. The value is a pointer, and it has to outlive the add.
    weight = ctypes.c_uint64(0xFFFFFFFFFFFFFFFF)
    # FWPM_LAYER_ALE_AUTH_CONNECT_V4 and V6. Loopback is both families.
    layers = (
        ("v4", guid(0xC38D57D1, 0x05A7, 0x4C33, (0x90, 0x4F, 0x7F, 0xBC, 0xEE, 0xE6, 0x0E, 0x82))),
        ("v6", guid(0x4A72393B, 0x319F, 0x44BC, (0x84, 0xC3, 0xBA, 0x54, 0xDC, 0xB3, 0xB6, 0xB4))),
    )
    universal = guid(0xEEBECC03, 0xCED4, 0x4380, (0x81, 0x9A, 0x27, 0x34, 0x39, 0x7B, 0x2B, 0x74))
    kept = []
    for label, layer in layers:
        condition = Condition()
        # FWPM_CONDITION_ALE_PACKAGE_ID
        condition.fieldKey = guid(0x71BC78FA, 0xF17C, 0x4997, (0xA6, 0x02, 0x6A, 0xBB, 0x26, 0x1F, 0x35, 0x1C))
        condition.matchType = 0  # FWP_MATCH_EQUAL
        condition.value.type = 13  # FWP_SID
        condition.value.ptr = lease.sid
        filt = Filter()
        filt.displayData.name = name
        filt.displayData.description = name
        filt.layerKey = layer
        filt.subLayerKey = universal
        filt.weight.type = 4  # FWP_UINT64
        filt.weight.ptr = ctypes.cast(ctypes.byref(weight), ctypes.c_void_p)
        filt.numFilterConditions = 1
        filt.filterCondition = ctypes.pointer(condition)
        filt.action.type = 0x1001  # FWP_ACTION_BLOCK
        added = ctypes.c_uint64()
        status = fwpu.FwpmFilterAdd0(engine, ctypes.byref(filt), None, ctypes.byref(added))
        if status != 0:
            raise SandboxError(f"the connect-layer block was not added for {label} (winerror {status})")
        _WFP_LAYERS[label] = True
        kept.append((condition, filt, added))
    # The session is dynamic, so closing the engine or exiting this process
    # removes the filters. Keep the handle, the session, and the strings alive
    # for the life of the process. Do not close the engine between jails.
    _WFP_ENGINE = (engine, fwpu, weight, kept, name, session)


def _firewall_block_command(sid_text: str) -> str:
    """PowerShell that installs the package-SID block.

    Windows PowerShell 5.1 formats an object piped to ``Out-Null``. The
    firewall rule object is large enough that two of those pipes run past
    the 60 second limit and the launch fails. Assigning to ``$null`` does
    not format the object.
    """
    if not sid_text.startswith("S-1-") or any(ch in sid_text for ch in "\"'$`;\r\n"):
        raise SandboxError("the AppContainer SID is not a form the firewall rule can use")
    return (
        "$names = @('strategybuilderjail-block-out','strategybuilderjail-block-in')\n"
        "foreach ($name in $names) {\n"
        "  Remove-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue\n"
        "}\n"
        "$null = New-NetFirewallRule -DisplayName 'strategybuilderjail-block-out' -Direction Outbound"
        f" -Action Block -Package '{sid_text}' -Profile Any -ErrorAction Stop\n"
        "$null = New-NetFirewallRule -DisplayName 'strategybuilderjail-block-in' -Direction Inbound"
        f" -Action Block -Package '{sid_text}' -Profile Any -ErrorAction Stop\n"
    )


def _win_block_network(sid_text: str) -> bool:
    """Block every inbound and outbound connection for this package SID.

    An AppContainer with no capabilities still reaches loopback far enough
    for a connect to time out instead of failing with access denied. A
    firewall block on the package SID fails that connect with 10013.
    ``netsh`` on current Windows rejects ``package=``, so this uses the
    firewall cmdlet.
    """
    command = _firewall_block_command(sid_text)
    system = os.environ.get("SystemRoot", "")
    powershell = Path(system) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe" if system else None
    if powershell is None or not powershell.is_file():
        raise SandboxError("powershell is not available; refusing to run without a network block")
    try:
        proc = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except OSError as exc:
        raise SandboxError(f"the AppContainer firewall block was not added: {exc}") from exc
    except subprocess.TimeoutExpired:
        # Both dynamic WFP layers are the block. A hung cmdlet is fail-closed
        # when either layer is missing. The caller does not try the cmdlet
        # again in this process. Cleanup still removes a rule the cmdlet
        # managed to add before it was killed.
        _arm_firewall_cleanup()
        if firewall_timeout_blocks_launch(_WFP_LAYERS.get("v4"), _WFP_LAYERS.get("v6")):
            raise SandboxError(
                "the firewall cmdlet timed out and the IPv4 and IPv6 connect filters are not both installed"
            )
        return False
    if proc.returncode != 0:
        detail = ((proc.stdout or "") + (proc.stderr or "")).strip()
        raise SandboxError(f"the AppContainer firewall block was not added: {detail[-500:]}")
    _arm_firewall_cleanup()
    return True


def _firewall_remove_command() -> str:
    """Delete the package rules this process added. ``$null`` avoids Out-Null."""
    return (
        "$names = @('strategybuilderjail-block-out','strategybuilderjail-block-in')\n"
        "foreach ($name in $names) {\n"
        "  $null = Remove-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue\n"
        "}\n"
    )


def _forget_firewall_marker() -> None:
    """The on-disk marker must not skip the next install after the rules are gone."""
    try:
        path = _grant_marker_path()
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, SandboxError):
        return
    if not isinstance(payload, dict) or not payload.get("firewall_blocked"):
        return
    payload["firewall_blocked"] = False
    try:
        path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        return


def _remove_firewall_rules() -> None:
    """Drop the persistent rules when this process is finished with them."""
    if sys.platform != "win32":
        return
    _forget_firewall_marker()
    system = os.environ.get("SystemRoot", "")
    powershell = Path(system) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe" if system else None
    if powershell is None or not powershell.is_file():
        return
    try:
        subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-Command", _firewall_remove_command()],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return


def _arm_firewall_cleanup() -> None:
    if getattr(_arm_firewall_cleanup, "armed", False):
        return
    _arm_firewall_cleanup.armed = True
    atexit.register(_remove_firewall_rules)


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


_ACL_MARKER_VERSION = 2


def _recorded_grants(sid_text: str, roots):
    path = _grant_marker_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if payload.get("version") != _ACL_MARKER_VERSION:
        return None
    if payload.get("sid") != sid_text:
        return None
    expected = [os.path.normcase(str(item)) for item in roots]
    recorded_roots = [os.path.normcase(str(item)) for item in payload.get("roots") or []]
    if recorded_roots != expected:
        return None
    allow = [str(item) for item in roots]
    exact, trees = protected_acl_layout(os.environ)
    grants = []
    for item in payload.get("grants") or []:
        if not isinstance(item, list) or len(item) != 2:
            return None
        target = Path(item[0])
        if acl_write_problem(target, allow, exact, trees) is not None:
            return None
        grants.append((target, bool(item[1])))
    return grants


def firewall_block_is_current(marker, sid: str) -> bool:
    """True when this machine already has the package firewall rules.

    A runtime copy refresh must not run ``New-NetFirewallRule`` again.
    That cmdlet holds the profile mutex, and on a GitHub runner it can
    sit until the 60 second limit.
    """
    return (
        isinstance(marker, dict)
        and marker.get("firewall_blocked") is True
        and marker.get("loopback_cleared") is True
        and marker.get("sid") == sid
    )


def _write_grant_marker(sid_text: str, grants, roots, *, loopback_cleared: bool, firewall_blocked: bool = True) -> None:
    payload = {
        "version": _ACL_MARKER_VERSION,
        "sid": sid_text,
        "roots": [str(path) for path in roots],
        "grants": [[str(path), bool(recurse)] for path, recurse in grants],
        "local_denied": False,
        "loopback_cleared": bool(loopback_cleared),
        "firewall_blocked": bool(firewall_blocked),
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
        container = _container_runtime_parent(lease)
        roots = _prepare_runtime_grant_roots(container)
        lease.acl_allow = _dedupe_acl(
            [str(path) for path in roots] + [str(path) for path in _CREATED_APP_DIRS]
        )
        # Read-only. A missing package ACE fails the grant before any write,
        # including on the marker fast path. An interpreter the container
        # cannot walk to is copied into the container's own folder. The
        # user profile and the toolcache are not edited.
        _require_unowned_runtime_access(roots)
        for root in roots:
            if _trusted_runtime_root(root):
                continue
            _require_existing_ancestor_traverse(root, lease.acl_allow)
        made: list[tuple[Path, bool]] = []
        try:
            made = _materialize_runtime_copies(lease)
        except Exception:
            lease.grant_paths = made
            try:
                for path, recurse in made:
                    _win_remove_aces(lease, [path], recurse=recurse)
            except Exception:
                pass
            raise
        recorded = _recorded_grants(sid_text, roots)
        marker = {}
        try:
            marker = json.loads(_grant_marker_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            marker = {}
        if (
            recorded is not None
            and firewall_block_is_current(marker, sid_text)
            and _runtime_copies_ready()
        ):
            lease.grant_paths = recorded
            return
        # Once per machine when the marker says the rules exist. A copy
        # refresh must not call the cmdlet again. A hung cmdlet is not
        # retried in this process; the WFP filter is already in place.
        global _FIREWALL_TRIED
        if firewall_block_is_current(marker, sid_text) or _FIREWALL_TRIED:
            firewall_blocked = firewall_block_is_current(marker, sid_text)
        else:
            _FIREWALL_TRIED = True
            _clear_loopback(name, sid_text)
            firewall_blocked = _win_block_network(sid_text)
        if recorded is not None and _runtime_copies_ready():
            lease.grant_paths = recorded
            _write_grant_marker(
                sid_text, recorded, roots, loopback_cleared=True, firewall_blocked=firewall_blocked
            )
            return
        grants: list[tuple[Path, bool]] = list(made)
        try:
            for root in roots:
                recurse = _runtime_grant_recurse(root)
                _win_grant(lease, root, "(OI)(CI)(RX)", recurse=recurse)
                grants.append((root, recurse))
        except Exception:
            lease.grant_paths = grants
            try:
                for path, recurse in grants:
                    _win_remove_aces(lease, [path], recurse=recurse)
            except Exception:
                pass
            raise
        lease.grant_paths = grants
        _write_grant_marker(
            sid_text, grants, roots, loopback_cleared=True, firewall_blocked=firewall_blocked
        )

    try:
        # Every process that launches a jail holds this filter. A scrubbed
        # worker is a new process, so the grant marker cannot stand in for it.
        _win_wfp_block(lease)
        _with_profile_lock(ensure_grants)
    except Exception:
        advapi.FreeSid(sid)
        if created:
            userenv.DeleteAppContainerProfile(name)
        raise


# 8.3 names Windows still opens as the protected directories.
_ACL_SHORT_NAMES = {
    "progra~1": "Program Files",
    "progra~2": "Program Files (x86)",
    "progra~3": "Program Files",
    "window~1": "Windows",
    "system~1": "System32",
    "syswow~1": "SysWOW64",
}


def canonicalize_acl_path(path) -> str | None:
    """The path Windows will open, or None when it must not be an allow root.

    ``\\\\?\\`` and ``\\\\.\\`` prefixes are stripped. UNC paths are rejected.
    ``..`` is collapsed before trailing dots and spaces are stripped, because
    ``"..".rstrip(" .")`` is empty and would otherwise delete the component.
    ``.`` is dropped. Known 8.3 names of Program Files, Windows, System32,
    and SysWOW64 are expanded. A path that climbs above its drive is rejected.
    """
    text = str(path).strip().replace("/", "\\")
    folded = text.upper()
    if folded.startswith("\\\\?\\UNC\\") or folded.startswith("\\\\.\\UNC\\"):
        return None
    for prefix in ("\\\\?\\", "\\\\.\\", "\\??\\"):
        if folded.startswith(prefix):
            text = text[len(prefix) :]
            folded = text.upper()
            break
    if text.startswith("\\\\"):
        return None
    if len(text) >= 2 and text[1] == ":":
        drive = text[:2].upper()
        rest = text[2:]
    else:
        drive = ""
        rest = text
    parts: list[str] = []
    for part in rest.split("\\"):
        if part in ("", "."):
            continue
        # Parent references are collapsed before the trailing-dot strip.
        # ``"..".rstrip(" .")`` is empty, so stripping first drops the component
        # and ``C:\\Users\\Jeu\\Desktop\\..\\..\\Windows`` stays under Desktop.
        if part.rstrip(" ") == "..":
            if not parts:
                return None
            parts.pop()
            continue
        cleaned = part.rstrip(" .")
        if not cleaned or cleaned == ".":
            continue
        if cleaned == "..":
            if not parts:
                return None
            parts.pop()
            continue
        short = cleaned.casefold()
        if short in _ACL_SHORT_NAMES:
            cleaned = _ACL_SHORT_NAMES[short]
        parts.append(cleaned)
    if drive:
        if not parts:
            return drive + "\\"
        return drive + "\\" + "\\".join(parts)
    if not parts:
        return None
    return "\\".join(parts)


def _split_acl_path(path) -> tuple[str, tuple[str, ...]]:
    """Drive plus components. Backslashes are separators even on Linux tests.

    A rejected path (UNC, or a ``..`` that climbs off the drive) is a UNC
    sentinel so it cannot compare equal to an allow root.
    """
    canon = canonicalize_acl_path(path)
    if canon is None:
        return "\\\\", ("rejected",)
    text = canon
    if len(text) >= 2 and text[1] == ":":
        drive = text[:2].upper()
        rest = text[2:].lstrip("\\")
        parts = tuple(part for part in rest.split("\\") if part)
        return drive, parts
    parts = tuple(part for part in text.split("\\") if part)
    return "", parts


def _acl_parts(path) -> tuple[str, tuple[str, ...]]:
    drive, parts = _split_acl_path(path)
    return drive, tuple(part.casefold() for part in parts)


def acl_same(left, right) -> bool:
    return _acl_parts(left) == _acl_parts(right)


def _acl_inside(path, root) -> bool:
    drive, parts = _acl_parts(path)
    root_drive, root_parts = _acl_parts(root)
    if drive != root_drive:
        return False
    return len(parts) > len(root_parts) and parts[: len(root_parts)] == root_parts


def _acl_join(drive: str, parts: tuple[str, ...]) -> str:
    if not parts:
        return f"{drive}\\" if drive and drive not in {"\\\\"} else drive
    if drive == "\\\\":
        return "\\\\" + "\\".join(parts)
    if not drive:
        return "\\".join(parts)
    return drive + "\\" + "\\".join(parts)


def acl_ancestor_paths(path) -> list[str]:
    """Parents of ``path``, including the volume root. Does not include ``path``."""
    drive, parts = _split_acl_path(path)
    found = []
    for length in range(len(parts) - 1, 0, -1):
        found.append(_acl_join(drive, parts[:length]))
    if drive and drive != "\\\\":
        found.append(drive + "\\")
    return found


def _is_volume_acl_path(path) -> bool:
    drive, parts = _acl_parts(path)
    return bool(drive) and not parts


def _interpreter_for_acl(environ) -> str:
    """The interpreter prefix whose toolcache ancestors stay protected."""
    raw = (environ or {}).get("SB_PYTHON_PREFIX") or ""
    if raw:
        return str(raw)
    base = str(getattr(sys, "base_prefix", "") or "")
    if "hostedtoolcache" in base.casefold().replace("/", "\\"):
        return base
    return ""


def toolcache_ancestor_paths(interpreter) -> list[str]:
    """Ancestors of a toolcache interpreter, not the interpreter directory.

    ``C:\\hostedtoolcache`` and ``...\\Python`` are protected. The version
    directory that holds ``python.exe`` is not in this list, so it can still
    be an allow root.
    """
    if not interpreter:
        return []
    canon = canonicalize_acl_path(interpreter)
    if not canon:
        return []
    drive, parts = _split_acl_path(canon)
    folded = [part.casefold() for part in parts]
    if "hostedtoolcache" not in folded:
        return []
    index = folded.index("hostedtoolcache")
    return [_acl_join(drive, parts[:length]) for length in range(index + 1, len(parts))]


def protected_acl_layout(environ) -> tuple[list[str], list[str]]:
    """Exact directories that must never be edited, and trees that are off limits.

    Program Files is exact: the directory itself is protected, and a frozen
    bundle underneath can still be an allow root. The Windows directory and
    System32 are trees: nothing inside them is an allow root. ``Packages``
    is exact, so the profile folder created under it can still be granted.
    Toolcache ancestors of the interpreter are exact. The interpreter
    directory itself is not.
    """
    exact: list[str] = []
    trees: list[str] = []

    def add_exact(raw) -> None:
        if raw:
            exact.append(str(raw))

    system = environ.get("SystemRoot") or environ.get("WINDIR")
    if system:
        add_exact(system)
        trees.append(str(system))
        root = str(system).rstrip("\\/")
        add_exact(root + "\\System32")
        add_exact(root + "\\SysWOW64")
        trees.append(root + "\\System32")
        trees.append(root + "\\SysWOW64")
    for key in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        add_exact(environ.get(key, ""))
    drive = environ.get("SystemDrive") or ""
    letter = str(drive).rstrip("\\/")
    if len(letter) >= 2 and letter[1] == ":":
        add_exact(letter[:2] + "\\")
    profile = environ.get("USERPROFILE")
    if profile:
        add_exact(profile)
    local = environ.get("LOCALAPPDATA") or ""
    if local:
        canon = canonicalize_acl_path(local)
        if canon:
            add_exact(canon.rstrip("\\") + "\\Packages")
    for ancestor in toolcache_ancestor_paths(_interpreter_for_acl(environ)):
        add_exact(ancestor)
    return exact, trees


def _inside_program_files(path, environ) -> bool:
    for key in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        root = environ.get(key) or ""
        if root and (acl_same(path, root) or _acl_inside(path, root)):
            return True
    return False


def _can_be_allow_root(path, environ) -> bool:
    drive, _parts = _split_acl_path(path)
    if drive == "\\\\":
        return False
    exact, trees = protected_acl_layout(environ)
    if any(acl_same(path, banned) for banned in exact) or _is_volume_acl_path(path):
        return False
    if any(acl_same(path, tree) or _acl_inside(path, tree) for tree in trees):
        return False
    return True


def runtime_root_is_app_owned(path, *, environ, venv: bool = False, frozen: bool = False, active: bool = False) -> bool:
    """True when this process may edit the DACL of an interpreter directory.

    ``C:\\Windows`` is not owned, including when a candidate equals
    ``SystemRoot`` or sits inside it. An interpreter under Program Files is
    not owned either: those directories already carry All Application
    Packages. A virtualenv, a frozen bundle outside the Windows tree, and
    the running interpreter when it lives somewhere else (the GitHub
    toolcache, a user install, a venv) are the copies the old icacls grant
    edited and that this process still may edit.
    """
    if not _can_be_allow_root(path, environ):
        return False
    if _inside_program_files(path, environ):
        return bool(frozen)
    return bool(venv or frozen or active)


def acl_write_problem(path, allow_roots, exact, trees) -> str | None:
    """None when ``path`` may be edited. Otherwise a short reason."""
    if any(acl_same(path, banned) for banned in exact) or _is_volume_acl_path(path):
        return "protected directory"
    if any(acl_same(path, tree) or _acl_inside(path, tree) for tree in trees):
        return "inside a system directory"
    if not any(acl_same(path, root) or _acl_inside(path, root) for root in allow_roots):
        return "outside the app-owned allow-list"
    return None


def acl_revoke_problem(path, *, environ) -> str | None:
    """A cleanup write is still a write. System directories are never edited."""
    exact, trees = protected_acl_layout(environ)
    if any(acl_same(path, banned) for banned in exact) or _is_volume_acl_path(path):
        return "protected directory"
    if any(acl_same(path, tree) or _acl_inside(path, tree) for tree in trees):
        return "inside a system directory"
    return None


def _resolved_acl_target(path) -> str:
    """The path ``CreateFileW`` would open, when this process can open it.

    A missing path stays lexical. ``canonicalize_acl_path`` still collapses
    ``..`` and expands the short names. An existing path is the final DOS
    path from ``GetFinalPathNameByHandleW``, which follows ``..`` the same
    way the ACL write's open does.
    """
    text = str(path)
    if sys.platform != "win32":
        return text
    try:
        if not os.path.exists(text):
            return text
    except OSError:
        return text
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = ctypes.WINFUNCTYPE(
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        use_last_error=True,
    )(("CreateFileW", kernel))
    final_name = ctypes.WINFUNCTYPE(
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        use_last_error=True,
    )(("GetFinalPathNameByHandleW", kernel))
    close = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p)(("CloseHandle", kernel))
    # FILE_READ_ATTRIBUTES, share read/write/delete, OPEN_EXISTING, BACKUP_SEMANTICS.
    handle = create(text, 0x80, 0x7, None, 3, 0x02000000, None)
    invalid = ctypes.c_void_p(-1).value
    if not handle or int(handle) == int(invalid):
        return text
    try:
        size = 32768
        buf = ctypes.create_unicode_buffer(size)
        written = int(final_name(handle, buf, size, 0))
        if written <= 0 or written >= size:
            return text
        return buf.value
    finally:
        close(handle)


def refuse_acl_target(path, *, allow, environ) -> None:
    """Raise when a grant would edit a directory this app must not touch."""
    checked = _resolved_acl_target(path)
    exact, trees = protected_acl_layout(environ)
    problem = acl_write_problem(checked, allow, exact, trees)
    if problem:
        raise SandboxError(f"refusing to edit the ACL on {path}: {problem}")


def should_write_ace(already_present: bool) -> bool:
    """An allow ACE that already covers the mask is not written again."""
    return not already_present


def _dedupe_acl(paths) -> list[str]:
    kept: list[str] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for raw in paths:
        if raw is None or raw == "":
            continue
        key = _acl_parts(raw)
        if key in seen:
            continue
        seen.add(key)
        kept.append(str(raw))
    return kept


def _flagged(path, group) -> bool:
    return any(acl_same(path, item) for item in group)


def acl_allow_roots(
    candidates,
    *,
    environ,
    work=None,
    active=(),
    venv=(),
    frozen=(),
    app_data=(),
) -> list[str]:
    """App-owned grant roots from the historical icacls candidate list."""
    allow: list[str] = []
    for raw in candidates:
        if not runtime_root_is_app_owned(
            raw,
            environ=environ,
            venv=_flagged(raw, venv),
            frozen=_flagged(raw, frozen),
            active=_flagged(raw, active),
        ):
            continue
        allow.append(str(raw))
    for raw in list(app_data) + ([] if work is None else [work]):
        if _can_be_allow_root(raw, environ):
            allow.append(str(raw))
    allow = _dedupe_acl(allow)
    allow.sort(key=_acl_parts)
    return allow


def planned_acl_targets(
    candidates,
    *,
    environ,
    work=None,
    active=(),
    venv=(),
    frozen=(),
    app_data=(),
) -> list[str]:
    """Historical icacls targets (candidates and their ancestors) after the filter.

    Ancestors are considered and then dropped. A protected directory that
    was an explicit candidate, including ``SystemRoot``, is dropped too.
    """
    allow = acl_allow_roots(
        candidates,
        environ=environ,
        work=work,
        active=active,
        venv=venv,
        frozen=frozen,
        app_data=app_data,
    )
    exact, trees = protected_acl_layout(environ)
    proposed: list[str] = []
    for raw in list(candidates) + list(app_data) + ([] if work is None else [work]):
        if raw is None or raw == "":
            continue
        proposed.append(str(raw))
        proposed.extend(acl_ancestor_paths(raw))
    kept = []
    for path in proposed:
        if not any(acl_same(path, root) for root in allow):
            continue
        if acl_write_problem(path, allow, exact, trees) is not None:
            continue
        kept.append(path)
    kept = _dedupe_acl(kept)
    kept.sort(key=_acl_parts)
    return kept


def _is_volume_root(path: Path) -> bool:
    """True for ``/`` or ``C:\\``. A volume root is never an ACL target."""
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    return resolved.parent == resolved


def _system_root() -> Path | None:
    raw = os.environ.get("SystemRoot")
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_dir() else None


def _interpreter_candidates() -> list[Path]:
    frozen_root = getattr(sys, "_MEIPASS", None)
    raws = [sys.base_prefix, sys.prefix, os.path.dirname(sys.executable), frozen_root]
    found: list[Path] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for raw in raws:
        if not raw:
            continue
        path = Path(raw)
        try:
            path = path.resolve()
        except OSError:
            pass
        try:
            if not path.is_dir():
                continue
        except OSError:
            continue
        key = _acl_parts(path)
        if key in seen:
            continue
        seen.add(key)
        found.append(path)
    return found


def _grant_candidates() -> list[str]:
    """Historical icacls targets, before the app-owned filter.

    ``SystemRoot`` (``C:\\Windows`` on a normal install) is appended here
    as its own candidate. It is not produced by walking parents of the
    interpreter. On a GitHub runner ``sys.base_prefix`` is under
    ``C:\\hostedtoolcache``, and that walk never reaches ``C:\\Windows``.
    The filter drops ``SystemRoot``, so the DACL write is not attempted.
    """
    found = [str(path) for path in _interpreter_candidates()]
    system = _system_root()
    if system is not None:
        found.append(str(system))
    seen = {_acl_parts(item) for item in found}
    for package in _runtime_package_dirs():
        directory = package if package.is_dir() else package.parent
        key = _acl_parts(directory)
        if key in seen:
            continue
        try:
            if not directory.is_dir():
                continue
        except OSError:
            continue
        seen.add(key)
        found.append(str(directory))
    return found


def _windows_grant_roots() -> list[Path]:
    """Interpreter directories whose DACLs this process may edit.

    User-site is not a root. It depends on the process environment, so a
    scrubbed worker would miss the marker and walk the tree again. A
    package directory inside an owned prefix is covered by that grant and
    is not a separate target.
    """
    interpreters = _interpreter_candidates()
    active = {str(path) for path in interpreters}
    frozen_raw = getattr(sys, "_MEIPASS", None)
    frozen = set()
    if frozen_raw:
        frozen.add(str(frozen_raw))
    venv = set()
    for path in interpreters:
        prefix_differs = _acl_same_prefix(path)
        if (path / "pyvenv.cfg").is_file() or prefix_differs:
            venv.add(str(path))
    kept = planned_acl_targets(
        _grant_candidates(),
        environ=os.environ,
        active=active,
        venv=venv,
        frozen=frozen,
    )
    return [Path(path) for path in kept]


def _acl_same_prefix(path: Path) -> bool:
    if not sys.prefix or not sys.base_prefix:
        return False
    return acl_same(path, sys.prefix) and not acl_same(sys.prefix, sys.base_prefix)


# (source, dest) for an interpreter the container cannot walk to.
# The dest is an app-owned copy. The source's ancestors are not edited.
_RUNTIME_COPIES: list[tuple[str, str]] = []
_FIREWALL_TRIED = False
_RUNTIME_COPY_VERSION = "2"
# Set when the copy parent is the AppContainer folder. Ancestors of that
# folder are not granted and are not required to show a package ACE.
_CONTAINER_FOLDER_NOTE = ""
_TRUSTED_STAGE_ROOTS: list[str] = []
# Parent of the staged runtime. The private directory is created here when
# ProgramData and Public are not already traversable.
_RUNTIME_PARENT = ""


def _child_executable() -> str:
    """Interpreter the AppContainer starts.

    A copy under an app-owned directory replaces an interpreter whose
    ancestors the container cannot traverse. Those ancestors are not edited.
    The string is not passed through ``Path``. On Windows that rewrites a
    POSIX path, including when a runtime copy exists and this executable
    is not one of the copied prefixes.
    """
    if not _RUNTIME_COPIES:
        return sys.executable
    return translate_staged_path(sys.executable, _RUNTIME_COPIES)


def first_blocked_ancestor(path, allow, accessible) -> str | None:
    """Nearest unowned ancestor ``accessible`` rejects, or None.

    Ancestors that are an allow root, or that sit inside one, are skipped.
    The volume root is skipped too: it is never granted, and a missing
    explicit ACE there is not a reason to refuse a copy whose other
    ancestors already let the container through. This does not write an ACL.
    """
    for parent in acl_ancestor_paths(path):
        if _is_volume_acl_path(parent):
            continue
        if any(acl_same(parent, root) or _acl_inside(parent, root) for root in allow):
            continue
        if accessible(parent):
            continue
        return parent
    return None


def stage_unreachable_runtimes(roots, *, allow, accessible, stage_root, trust_stage: bool = False):
    """Grant roots after an unreachable interpreter is replaced by a copy.

    ``copies`` is ``(source, dest)``. A reachable root stays. An unreachable
    root is omitted and its dest, under ``stage_root``, is granted instead.
    ``stage_root`` itself is granted when several copies sit under it.
    When no parent is available for the copy, the error names the blocked
    ancestor and that ancestor is not added as a target.
    """
    blocked = []
    grant = []
    for root in roots:
        if root is None or root == "":
            continue
        parent = first_blocked_ancestor(str(root), allow, accessible)
        if parent is None:
            grant.append(str(root))
        else:
            blocked.append((str(root), parent))
    if not blocked:
        kept = _dedupe_acl(grant)
        kept.sort(key=_acl_parts)
        return kept, []
    if not stage_root:
        raise SandboxError(
            f"the AppContainer cannot traverse {blocked[0][1]} "
            f"({traverse_trace(blocked[0][0], accessible)}); "
            "this process will not rewrite a directory it does not own"
        )
    stage_blocked = None if trust_stage else first_blocked_ancestor(str(stage_root), allow, accessible)
    if stage_blocked is not None:
        raise SandboxError(
            f"the AppContainer cannot traverse {stage_blocked}; "
            "this process will not rewrite a directory it does not own"
        )
    if len(blocked) == 1:
        dests = [str(stage_root)]
    else:
        dests = [str(Path(str(stage_root)) / str(index)) for index in range(len(blocked))]
        grant.append(str(stage_root))
    copies = []
    for (source, _parent), dest in zip(blocked, dests):
        copies.append((source, dest))
        grant.append(dest)
    kept = _dedupe_acl(grant)
    kept.sort(key=_acl_parts)
    return kept, copies


def traverse_trace(path, accessible) -> str:
    """Short read-only note of which ancestors already allow traverse."""
    notes = []
    for item in acl_ancestor_paths(path):
        if _is_volume_acl_path(item):
            state = "volume"
        elif accessible(item):
            state = "open"
        else:
            state = "closed"
        notes.append(f"{item}={state}")
        if len(notes) >= 5:
            break
    return "; ".join(notes)


def translate_staged_path(path, copies) -> str:
    """Map a path through the longest staged interpreter prefix."""
    text = str(path)
    best = None
    best_len = -1
    for source, dest in copies:
        if not (acl_same(text, source) or _acl_inside(text, source)):
            continue
        length = len(_acl_parts(source)[1])
        if length > best_len:
            best = (str(source), str(dest))
            best_len = length
    if best is None:
        return text
    source, dest = best
    _drive, parts = _split_acl_path(text)
    _source_drive, source_parts = _split_acl_path(source)
    relative = parts[len(source_parts) :]
    dest_drive, dest_parts = _split_acl_path(dest)
    return _acl_join(dest_drive, dest_parts + relative)


def rewrite_pyvenv_home(text: str, translate) -> str:
    """Point a copied virtualenv at the staged base prefix."""
    lines = []
    for line in text.splitlines(keepends=True):
        newline = ""
        body = line
        if body.endswith("\r\n"):
            newline = "\r\n"
            body = body[:-2]
        elif body.endswith("\n"):
            newline = "\n"
            body = body[:-1]
        key, sep, value = body.partition("=")
        if sep and key.strip().casefold() == "home":
            home = value.strip()
            updated = str(translate(home))
            if updated != home:
                lines.append(f"{key}{sep} {updated}{newline}")
                continue
        lines.append(line)
    return "".join(lines)


def runtime_relpath_is_copied(parts, *, is_dir: bool, package_prefixes, full: bool = False) -> bool:
    """Files the child interpreter opens, not the whole site-packages tree.

    ``package_prefixes`` are casefolded relative paths of imported packages,
    such as ``Lib/site-packages/numpy``. A ``._pth`` file is left behind so
    the copy does not redirect the child at the original prefix. ``full``
    keeps a frozen bundle's layout, including directories other than Lib
    and DLLs, and still drops caches and path files.
    """
    folded = tuple(part.casefold() for part in parts)
    if any(
        part.casefold() == "__pycache__"
        or part.casefold().endswith(".dist-info")
        or part.casefold().endswith(".egg-info")
        for part in parts
    ):
        return False
    if folded and (folded[-1].endswith("._pth") or folded[-1].endswith(".pth")):
        return False
    if full:
        return True
    if not folded:
        return True
    # The CPython regression suite is not opened by the child. Copying it
    # dominates the time and is not granted as a separate tree.
    if len(folded) >= 2 and folded[0] == "lib" and folded[1] in {"test", "idlelib", "turtledemo"}:
        return False
    if len(folded) >= 2 and folded[0] == "lib" and folded[1] == "site-packages":
        for prefix in package_prefixes:
            if folded[: len(prefix)] == prefix or prefix[: len(folded)] == folded:
                return True
        return False
    if folded[0] in {"lib", "dlls"}:
        return True
    return len(folded) == 1 and not is_dir


def _translate_runtime(path) -> Path:
    if not _RUNTIME_COPIES:
        return Path(path)
    return Path(translate_staged_path(path, _RUNTIME_COPIES))


def _staged_source(path) -> bool:
    for source, _dest in _RUNTIME_COPIES:
        if acl_same(path, source) or _acl_inside(path, source):
            return True
    return False


def _ancestor_accessible(path) -> bool:
    return _existing_package_access(path, read=False, traverse=True)


def _runtime_stamp_path() -> Path:
    return _grant_marker_path().with_name("strategybuilderjail-runtime.json")


def _load_runtime_stamp() -> list:
    try:
        payload = json.loads(_runtime_stamp_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict) or payload.get("version") != _RUNTIME_COPY_VERSION:
        return []
    copies = payload.get("copies")
    return copies if isinstance(copies, list) else []


def runtime_tree_digest(root: Path) -> str:
    """Hash of every file under ``root``. A changed byte changes the digest."""
    digest = hashlib.sha256()
    root = Path(root)
    if not root.exists():
        digest.update(b"missing")
        return digest.hexdigest()
    paths = []
    if root.is_file():
        paths = [root]
    else:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            for name in sorted(filenames):
                paths.append(Path(dirpath) / name)
    for path in paths:
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        data = None
        for _attempt in range(3):
            try:
                data = path.read_bytes()
                break
            except OSError:
                data = None
        if data is None:
            digest.update(b"unreadable\0")
            continue
        digest.update(str(len(data)).encode("ascii"))
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
    return digest.hexdigest()


def _remember_runtime_stamp() -> None:
    payload = {
        "version": _RUNTIME_COPY_VERSION,
        "copies": [
            {
                "source": source,
                "dest": dest,
                "sha256": runtime_tree_digest(Path(dest)),
            }
            for source, dest in _RUNTIME_COPIES
        ],
    }
    _runtime_stamp_path().write_text(json.dumps(payload), encoding="utf-8")


def _runtime_tree_present(dest: Path, src: Path) -> bool:
    """The files that identify this source are in the copy.

    A frozen launch copies every blocked root, and only the root that
    holds the executable has that executable. A second root is still a
    copy that the hash has to cover.
    """
    dest = Path(dest)
    src = Path(src)
    if not dest.exists():
        return False
    if getattr(sys, "frozen", False):
        exe_name = Path(sys.executable).name
        if src.is_file():
            return dest.is_file() and dest.name == exe_name
        if (src / exe_name).is_file():
            return (dest / exe_name).is_file()
        return True
    exe_name = "python.exe" if os.name == "nt" else "python"
    if (src / exe_name).is_file():
        return (dest / exe_name).is_file() and (dest / "Lib" / "os.py").is_file()
    return True


def _stamp_matches(dest: Path, src: Path, digest: str) -> bool:
    for item in _load_runtime_stamp():
        if not isinstance(item, dict):
            continue
        if item.get("sha256") != digest:
            continue
        if acl_same(item.get("source", ""), src) and acl_same(item.get("dest", ""), dest):
            return True
    return False


def _runtime_copy_ready(dest: Path, src: Path) -> bool:
    if not _runtime_tree_present(dest, src):
        return False
    digest = runtime_tree_digest(dest)
    if _stamp_matches(dest, src, digest):
        return True
    # A file can be unreadable for one pass while a scanner holds it.
    digest = runtime_tree_digest(dest)
    return _stamp_matches(dest, src, digest)


def _runtime_copies_ready() -> bool:
    for source, dest in _RUNTIME_COPIES:
        if not _runtime_copy_ready(Path(dest), Path(source)):
            return False
    return True


def _rewrite_staged_pyvenv(dest: Path) -> None:
    path = dest / "pyvenv.cfg"
    if not path.is_file():
        return
    text = path.read_text(encoding="utf-8")
    rewritten = rewrite_pyvenv_home(
        text, lambda home: translate_staged_path(home, _RUNTIME_COPIES)
    )
    if rewritten != text:
        path.write_text(rewritten, encoding="utf-8")


def _package_prefixes(src: Path, package_dirs) -> tuple:
    try:
        base = src.resolve()
    except OSError:
        base = src
    found = []
    for package in package_dirs:
        try:
            resolved = Path(package).resolve()
        except OSError:
            resolved = Path(package)
        try:
            relative = resolved.relative_to(base)
        except ValueError:
            continue
        found.append(tuple(part.casefold() for part in relative.parts))
    return tuple(found)


def _copy_runtime_into(src: Path, dest: Path, parts, prefixes, seen: set, *, full: bool) -> tuple[int, int]:
    """Copy every selected file, replacing one that is already there.

    Returns ``(expected, copied)``. A file that could not be read or written
    fails the copy. The caller stamps the tree only when those counts match.
    """
    try:
        entries = list(src.iterdir())
    except OSError as exc:
        raise SandboxError(f"could not read the interpreter at {src}: {exc}") from exc
    expected = 0
    copied = 0
    for entry in entries:
        relative = parts + (entry.name,)
        try:
            is_dir = entry.is_dir()
        except OSError as exc:
            raise SandboxError(f"could not read {entry}: {exc}") from exc
        if not runtime_relpath_is_copied(
            relative, is_dir=is_dir, package_prefixes=prefixes, full=full
        ):
            continue
        try:
            resolved = entry.resolve()
        except OSError:
            resolved = entry
        if resolved in seen:
            continue
        seen.add(resolved)
        target = dest / entry.name
        if is_dir:
            target.mkdir(exist_ok=True)
            child_expected, child_copied = _copy_runtime_into(
                entry, target, relative, prefixes, seen, full=full
            )
            expected += child_expected
            copied += child_copied
            continue
        expected += 1
        try:
            shutil.copy2(entry, target)
        except OSError as exc:
            raise SandboxError(
                f"could not copy {entry.name} into the app-owned interpreter: {exc}"
            ) from exc
        copied += 1
    return expected, copied


def _copy_runtime_tree(src: Path, dest: Path, package_dirs, *, full: bool = False) -> None:
    """Copy the interpreter files the child opens. The source tree is not granted.

    An existing file is replaced. The copy fails when any selected file was
    not written from the source, so the caller does not stamp a partial tree.
    """
    dest.mkdir(parents=True, exist_ok=True)
    prefixes = () if full else _package_prefixes(src, package_dirs)
    expected, copied = _copy_runtime_into(src, dest, (), prefixes, set(), full=full)
    if copied != expected:
        raise SandboxError(
            f"the staged interpreter at {dest} did not replace every file from {src}"
        )


def _copy_dest(path) -> bool:
    for _source, dest in _RUNTIME_COPIES:
        if acl_same(path, dest) or _acl_inside(path, dest):
            return True
    return False


def _trusted_runtime_root(path) -> bool:
    """A copy dest, or the container folder that holds it. Ancestors are not."""
    if _copy_dest(path):
        return True
    return any(acl_same(path, root) or _acl_inside(path, root) for root in _TRUSTED_STAGE_ROOTS)


# GetAppContainerFolderPath returns this when the profile folder is not
# created yet. Win32 ERROR_FILE_NOT_FOUND, as an HRESULT.
_FOLDER_NOT_FOUND = 0x80070002


def conventional_container_folder(environ, profile: str):
    """``%LOCALAPPDATA%\\Packages\\<profile>\\AC``, or None when unset.

    This is the path ``GetAppContainerFolderPath`` returns once the folder
    exists. The caller creates only the missing descendants of an existing
    Packages directory. Packages itself is not created here.
    """
    local = (environ or {}).get("LOCALAPPDATA") or ""
    if not local or not profile:
        return None
    return str(Path(local) / "Packages" / str(profile) / "AC")


def _container_runtime_parent(lease) -> Path | None:
    """Path from ``GetAppContainerFolderPath``. This does not create or grant it.

    The directory is this profile's own folder. ProgramData, Public, the
    toolcache, and the user-profile root are not this path, and none of
    them is granted here. A file-not-found result uses the conventional
    Packages path so the caller can create the missing profile folder.
    """
    global _CONTAINER_FOLDER_NOTE
    userenv = lease.userenv
    get_path = getattr(userenv, "GetAppContainerFolderPath", None)
    if get_path is None:
        _CONTAINER_FOLDER_NOTE = "GetAppContainerFolderPath is missing"
        return None
    get_path.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p)]
    get_path.restype = ctypes.c_long
    out = ctypes.c_void_p()
    hr = int(get_path(lease.sid_text, ctypes.byref(out)))
    text = ""
    if out.value:
        try:
            text = str(ctypes.wstring_at(out.value))
        finally:
            ole = ctypes.WinDLL("ole32")
            ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
            ole.CoTaskMemFree.restype = None
            ole.CoTaskMemFree(out)
    if hr == 0 and text:
        return Path(text)
    note = f"GetAppContainerFolderPath hresult {hr & 0xFFFFFFFF:08x}"
    if (hr & 0xFFFFFFFF) == _FOLDER_NOT_FOUND:
        fallback = conventional_container_folder(os.environ, _PROFILE_NAME)
        if fallback:
            _CONTAINER_FOLDER_NOTE = note
            return Path(fallback)
    _CONTAINER_FOLDER_NOTE = note
    return None


def container_mkdir_plan(folder, packages, exists) -> list[str] | None:
    """Missing directories to create so ``folder`` exists under ``packages``.

    ``packages`` must already exist. The plan lists only descendants that
    do not, outermost first. ``packages`` itself, the user-profile root,
    and the volume are never included. None means fail closed: the path
    is not inside ``packages``, or reaching it would create ``packages``
    or something above it.
    """
    if not folder or not packages:
        return None
    folder_text = str(folder)
    packages_text = str(packages)
    if not _acl_inside(folder_text, packages_text):
        return None
    packages_drive, packages_parts = _split_acl_path(packages_text)
    packages_norm = _acl_join(packages_drive, packages_parts)
    if not exists(packages_norm):
        return None
    missing: list[str] = []
    current = folder_text
    while not acl_same(current, packages_norm):
        drive, parts = _split_acl_path(current)
        if not parts:
            return None
        current_norm = _acl_join(drive, parts)
        if exists(current_norm):
            missing.reverse()
            return missing
        missing.append(current_norm)
        parent = _acl_join(drive, parts[:-1])
        if not parent or acl_same(parent, current_norm) or _is_volume_acl_path(parent):
            return None
        current = parent
    missing.reverse()
    return missing


def _dir_exists(path) -> bool:
    try:
        return Path(path).is_dir()
    except OSError:
        return False


def _accept_container_parent(folder: Path) -> Path | None:
    """Use the AppContainer folder ``GetAppContainerFolderPath`` returned.

    The usual path is ``%LOCALAPPDATA%\\Packages\\<profile>\\AC``. When
    ``<profile>`` or ``AC`` is missing and ``Packages`` already exists,
    only those missing descendants are created. ``Packages``, the
    user-profile root, and the volume are not created and are not granted.
    Only directories this call created are granted later. ``Packages``,
    the user-profile root, and the volume are not. A directory that
    already existed is left alone.
    """
    global _CONTAINER_FOLDER_NOTE
    runtime = folder / "runtime"
    if not _can_be_allow_root(folder, os.environ) or not _can_be_allow_root(runtime, os.environ):
        _CONTAINER_FOLDER_NOTE = f"container folder is not an allow root: {folder}"
        return None
    local = os.environ.get("LOCALAPPDATA") or ""
    packages = str(Path(local) / "Packages") if local else ""
    plan = container_mkdir_plan(folder, packages, _dir_exists) if packages else None
    if plan is not None and any(not _can_be_allow_root(raw, os.environ) for raw in plan):
        plan = None
    if plan is None:
        try:
            parent_ready = folder.parent.is_dir()
            folder_ready = folder.is_dir()
        except OSError as exc:
            _CONTAINER_FOLDER_NOTE = f"could not inspect the container folder: {exc}"
            return None
        if not parent_ready:
            note = f"container folder parent is missing: {folder}"
            if packages and _dir_exists(packages):
                note += "; Packages exists"
            elif packages:
                note += "; Packages is absent"
            _CONTAINER_FOLDER_NOTE = note
            return None
        plan = [] if folder_ready else [str(folder)]
    created: list[Path] = []
    try:
        for raw in plan:
            path = Path(raw)
            if not path.is_dir():
                path.mkdir(exist_ok=True)
                created.append(path)
            _reject_reparse_dir(path)
    except (OSError, SandboxError) as exc:
        _CONTAINER_FOLDER_NOTE = f"could not create the container folder: {exc}"
        for path in reversed(created):
            try:
                path.rmdir()
            except OSError:
                pass
        return None
    for directory in created:
        if not any(acl_same(directory, existing) for existing in _CREATED_APP_DIRS):
            _CREATED_APP_DIRS.append(directory)
    _CONTAINER_FOLDER_NOTE = str(folder)
    return folder


def _copy_failure_note() -> str:
    parts = []
    if _CONTAINER_FOLDER_NOTE:
        parts.append(_CONTAINER_FOLDER_NOTE)
    if sys.platform == "win32":
        for key in ("ProgramData", "PUBLIC", "ProgramFiles"):
            raw = os.environ.get(key, "")
            if not raw:
                continue
            state = "open" if _chain_has_package_traverse(Path(raw)) else "closed"
            parts.append(f"{raw}={state}")
    return "; ".join(parts)


def _prepare_runtime_grant_roots(container_dir: Path | None = None) -> list[Path]:
    """Owned grant roots. An unreachable interpreter is replaced by its copy.

    The per-user AppContainer folder is preferred. ProgramData, Public, a
    traversable ancestor, and Program Files are used only when that folder
    cannot be created and the shared parent has no inheritable write ACE.
    Ancestors this process did not create are not granted.
    """
    global _RUNTIME_COPIES, _TRUSTED_STAGE_ROOTS, _RUNTIME_PARENT
    _TRUSTED_STAGE_ROOTS = []
    _RUNTIME_PARENT = ""
    roots = [str(path) for path in _windows_grant_roots()]
    if sys.platform != "win32":
        _RUNTIME_COPIES = []
        return [Path(path) for path in roots]
    allow = _dedupe_acl(roots + [str(path) for path in _CREATED_APP_DIRS])
    blocked = any(first_blocked_ancestor(root, allow, _ancestor_accessible) for root in roots)
    stage_root = None
    trust = False
    if blocked:
        parent, trust = choose_copy_parent(
            container_dir, _accept_container_parent, _owned_work_parent
        )
        if parent is not None:
            stage_root = str((Path(parent) / "runtime").resolve())
            _RUNTIME_PARENT = str(Path(parent))
            if trust:
                # The profile directory created under Packages is reachable
                # for the same reason the container folder is: this process
                # owns it. Its ancestors are not granted and are not required
                # to show a package ACE.
                trusted = [str(Path(parent)), stage_root]
                trusted.extend(str(directory) for directory in _CREATED_APP_DIRS)
                _TRUSTED_STAGE_ROOTS = _dedupe_acl(trusted)
            allow = _dedupe_acl(allow + [str(path) for path in _CREATED_APP_DIRS] + [stage_root])
    try:
        grant, copies = stage_unreachable_runtimes(
            roots,
            allow=allow,
            accessible=_ancestor_accessible,
            stage_root=stage_root,
            trust_stage=trust,
        )
    except SandboxError as exc:
        extra = _copy_failure_note()
        if extra and extra not in str(exc):
            raise SandboxError(f"{exc} ({extra})") from exc
        raise
    _RUNTIME_COPIES = [(source, dest) for source, dest in copies]
    # The copy sits under directories this process just created. Those
    # directories do not inherit the ancestor's traverse ACE, so the
    # container cannot reach the copy unless they are granted too.
    # The ancestor itself is not in this list.
    if copies:
        for directory in _CREATED_APP_DIRS:
            if not any(acl_same(directory, path) for path in grant):
                grant.append(str(directory))
    return [Path(path) for path in grant]


def _remove_tree(path: Path) -> None:
    """Delete ``path``. A file that is still there fails the launch."""

    def onerror(func, name, _exc):
        try:
            os.chmod(name, 0o700)
            func(name)
        except OSError:
            pass

    if path.exists():
        shutil.rmtree(path, onerror=onerror)
    if path.exists():
        raise SandboxError(f"could not replace the staged interpreter at {path}")


def _runtime_acl_entries(lease, *, lock: bool):
    """Explicit ACEs for the staged interpreter.

    A locked tree grants the profile read-and-execute and denies write to
    Users, Everyone, and both package groups. SYSTEM keeps full control so
    the directory can still be removed by the OS. The owner keeps WRITE_DAC
    and replaces this DACL before the next copy.
    """

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

    advapi = lease.advapi
    kernel = lease.kernel
    advapi.ConvertStringSidToSidW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p)]
    advapi.ConvertStringSidToSidW.restype = ctypes.c_int
    sids = []

    def sid_for(text: str):
        pointer = ctypes.c_void_p()
        if not advapi.ConvertStringSidToSidW(text, ctypes.byref(pointer)) or not pointer.value:
            raise SandboxError(f"could not use SID {text}")
        sids.append(pointer)
        return pointer

    specs = []
    if lock:
        for text in ("S-1-5-32-545", "S-1-1-0", "S-1-15-2-1", "S-1-15-2-2"):
            specs.append((sid_for(text), _WRITE_DENY_MASK, 3))
        # The user who hashes the tree and owns it. Write stays denied
        # because this SID is also in Users, and that deny is checked
        # against the write bits. Read and execute are not those bits.
        user = _current_user_sid_text()
        if not user:
            raise SandboxError("could not read the current user SID")
        specs.append((sid_for(user), _RX_MASK, 1))
        specs.append((lease.sid, _RX_MASK, 1))
        specs.append((sid_for("S-1-5-18"), _FULL_MASK, 1))
    else:
        user = _current_user_sid_text()
        if not user:
            raise SandboxError("could not read the current user SID")
        specs.append((sid_for(user), _FULL_MASK, 1))
        specs.append((sid_for("S-1-5-18"), _FULL_MASK, 1))
    entries = (ExplicitAccess * len(specs))()
    for index, (sid, mask, mode) in enumerate(specs):
        entries[index].grfAccessPermissions = int(mask)
        entries[index].grfAccessMode = int(mode)
        entries[index].grfInheritance = _INHERIT_BOTH
        entries[index].Trustee.TrusteeForm = 0
        entries[index].Trustee.ptstrName = sid
    return entries, sids, kernel, ExplicitAccess


def _win_set_runtime_dacl(lease, root: Path, *, lock: bool) -> None:
    """Replace the DACL on the copy. ``lock`` drops inherited write."""
    if sys.platform != "win32" or not root.exists():
        return
    entries, sids, kernel, explicit = _runtime_acl_entries(lease, lock=lock)
    advapi = lease.advapi
    advapi.SetEntriesInAclW.argtypes = [
        ctypes.c_uint32,
        ctypes.POINTER(explicit),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi.SetEntriesInAclW.restype = ctypes.c_uint32
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    updated = ctypes.c_void_p()
    try:
        status = advapi.SetEntriesInAclW(len(entries), entries, None, ctypes.byref(updated))
        if status != 0 or not updated.value:
            raise SandboxError(f"could not build the runtime ACL (winerror {status})")
        pending = [root]
        while pending:
            current = pending.pop()
            _win_write_dacl(kernel, advapi, current, updated, protected=True)
            if not current.is_dir():
                continue
            try:
                pending.extend(path for path in current.iterdir() if not path.is_symlink())
            except OSError as exc:
                raise SandboxError(f"could not lock the runtime copy at {current}: {exc}") from exc
    finally:
        if updated.value:
            kernel.LocalFree(updated)
        for sid in sids:
            if sid.value:
                kernel.LocalFree(sid)


def _relock_runtime_copies(lease) -> None:
    """Put the locked DACL back, including when the tree hash already matches."""
    if sys.platform != "win32" or lease is None:
        return
    for _source, dest in _RUNTIME_COPIES:
        target = Path(dest)
        if target.exists():
            _win_set_runtime_dacl(lease, target, lock=True)


def _ensure_runtime_integrity(lease) -> None:
    """Recheck the staged tree immediately before CreateProcessW.

    A stamp that names the path is not enough. A changed file is deleted
    and copied again. A second mismatch fails the launch. A hash that
    already matches is not a reason to skip the locked DACL.
    """
    if not _RUNTIME_COPIES:
        return
    if not _runtime_copies_ready():
        _materialize_runtime_copies(lease)
        if not _runtime_copies_ready():
            raise SandboxError("the staged interpreter does not match its stamp")
    _relock_runtime_copies(lease)


def _materialize_runtime_copies(lease) -> list[tuple[Path, bool]]:
    """Copy interpreter files into the app-owned dest and lock the DACL.

    An unfinished or altered tree is deleted and replaced. The stamp stores
    a hash of the files. The protected DACL grants the package read and
    execute and denies write to Users, Everyone, and the package groups.
    The stamp lives beside the grant marker, outside the tree the child
    can read.
    """
    granted: list[tuple[Path, bool]] = []
    if not _RUNTIME_COPIES:
        return granted
    packages = _runtime_package_dirs()
    changed = False
    for source, dest in _RUNTIME_COPIES:
        src = Path(source)
        target = Path(dest)
        if _runtime_copy_ready(target, src):
            continue
        changed = True
        if target.exists():
            if sys.platform == "win32":
                _win_set_runtime_dacl(lease, target, lock=False)
            _remove_tree(target)
        target.mkdir(parents=True, exist_ok=True)
        _reject_reparse_dir(target)
        note_phase(f"runtime-copy-start {target.name}")
        _copy_runtime_tree(src, target, packages, full=bool(getattr(sys, "frozen", False)))
        _rewrite_staged_pyvenv(target)
        note_phase(f"runtime-copy-done {target.name}")
        if sys.platform == "win32":
            _win_set_runtime_dacl(lease, target, lock=True)
            granted.append((target, True))
    if changed:
        _remember_runtime_stamp()
    return granted


def _runtime_grant_recurse(root: Path) -> bool:
    """A directory that only holds staged copies is not walked."""
    text = str(root)
    dests = [dest for _source, dest in _RUNTIME_COPIES]
    if any(acl_same(text, dest) for dest in dests):
        return True
    if any(_acl_inside(dest, text) for dest in dests):
        return False
    return True


def _require_existing_ancestor_traverse(path, allow) -> None:
    """Fail when an unowned ancestor has no package traverse ACE. Never writes."""
    blocked = first_blocked_ancestor(str(path), allow, _ancestor_accessible)
    if blocked is not None:
        raise SandboxError(
            f"the AppContainer cannot traverse {blocked}; this process will not rewrite a directory it does not own"
        )


def _require_unowned_runtime_access(owned: list[Path]) -> None:
    """System interpreters stay readable only through ACEs Windows already has."""
    paths = list(_interpreter_candidates())
    for package in _runtime_package_dirs():
        directory = package if package.is_dir() else package.parent
        paths.append(directory)
    for path in paths:
        if _staged_source(path):
            continue
        if any(acl_same(path, root) or _acl_inside(path, root) for root in owned):
            continue
        try:
            if not path.exists():
                continue
        except OSError:
            continue
        if not _existing_package_access(path, read=True, traverse=True):
            raise SandboxError(
                f"the AppContainer cannot read {path}; this process will not rewrite a directory it does not own"
            )
        _require_existing_ancestor_traverse(path, owned)


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
        proc = subprocess.run(args, capture_output=True, text=True, timeout=45, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SandboxError(f"icacls did not finish: {exc}") from exc
    if proc.returncode != 0:
        detail = ((proc.stdout or "") + (proc.stderr or "")).strip()
        raise SandboxError(f"icacls failed ({proc.returncode}): {detail[-500:]}")


def _lease_allow(lease: _WinLease) -> list[str]:
    if lease.acl_allow:
        return list(lease.acl_allow)
    return [str(path) for path in _windows_grant_roots()]


def _refuse_acl_write(lease: _WinLease, path: Path) -> None:
    refuse_acl_target(path, allow=_lease_allow(lease), environ=os.environ)


# FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS
_OPEN_REPARSE = 0x00200000 | 0x02000000
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
# PROTECTED_DACL_SECURITY_INFORMATION. Inherited ACEs are not kept.
_PROTECTED_DACL = 0x80000000
# FILE_WRITE_DATA | APPEND | WRITE_EA | DELETE_CHILD | WRITE_ATTRIBUTES
# | DELETE | WRITE_DAC | WRITE_OWNER | GENERIC_WRITE | GENERIC_ALL
# The generic bits are for detecting an inheritable write. They are not
# put on a deny ACE: a GENERIC_ALL deny also denies read and the owner
# can no longer hash or list the tree.
_WRITE_MASK = 0x2 | 0x4 | 0x10 | 0x40 | 0x100 | 0x10000 | 0x40000 | 0x80000 | 0x40000000 | 0x10000000
_WRITE_DENY_MASK = 0x2 | 0x4 | 0x10 | 0x40 | 0x100 | 0x10000 | 0x40000 | 0x80000
# OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE
_INHERIT_TO_CHILDREN = 0x1 | 0x2


def reparse_point_blocks_acl(attributes: int, reparse_tag: int) -> bool:
    """A junction or symlink is not a directory this process may grant."""
    return bool(int(attributes) & _FILE_ATTRIBUTE_REPARSE_POINT) or bool(int(reparse_tag))


def dacl_has_inheritable_write(entries) -> bool:
    """True when an allow ACE would give a new child write access.

    ``entries`` is ``(ace_type, ace_flags, mask)``. Inherit-only is not
    required. Object-inherit or container-inherit on an allow is enough.
    """
    for ace_type, ace_flags, mask in entries:
        if int(ace_type) not in (0, 5):
            continue
        if not (int(ace_flags) & _INHERIT_TO_CHILDREN):
            continue
        if int(mask) & _WRITE_MASK:
            return True
    return False


def owner_matches(owner_sid, user_sid) -> bool:
    """An adopted directory is usable only when this process owns it."""
    if not owner_sid or not user_sid:
        return False
    return str(owner_sid) == str(user_sid)


def shared_parent_usable(*, traversable: bool, inheritable_write: bool, created: bool, owner_ok: bool) -> bool:
    """ProgramData or Public may hold the copy only with no inherited write.

    A directory this call creates is owned by this process. A directory
    that already existed has to be owned by this process as well.
    """
    if not traversable or inheritable_write:
        return False
    if created:
        return True
    return bool(owner_ok)


def choose_copy_parent(container_dir, accept_container, owned_work):
    """Prefer the per-user container folder. A shared parent is the fallback.

    Returns ``(parent, trusted)``. ``trusted`` is true for the container
    folder, whose ancestors are not granted.
    """
    if container_dir is not None:
        parent = accept_container(container_dir)
        if parent is not None:
            return parent, True
    return owned_work(), False


def _win_open_noreparse(kernel, path: Path):
    """Open a directory without following a junction. Reject a reparse point."""
    kernel.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.GetFileInformationByHandleEx.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    kernel.GetFileInformationByHandleEx.restype = ctypes.c_int
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    # READ_CONTROL | WRITE_DAC. Backup semantics so a directory can be opened.
    handle = kernel.CreateFileW(
        str(path),
        0x20000 | 0x40000,
        0x7,
        None,
        3,
        _OPEN_REPARSE,
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if not handle or int(handle) == int(invalid):
        raise _win_fail(f"could not open {path} without following a reparse point")

    class _Tag(ctypes.Structure):
        _fields_ = (("FileAttributes", ctypes.c_uint32), ("ReparseTag", ctypes.c_uint32))

    info = _Tag()
    # FileAttributeTagInfo = 9
    if not kernel.GetFileInformationByHandleEx(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
        kernel.CloseHandle(handle)
        raise _win_fail(f"could not read reparse information for {path}")
    if reparse_point_blocks_acl(int(info.FileAttributes), int(info.ReparseTag)):
        kernel.CloseHandle(handle)
        raise SandboxError(f"refusing to edit the ACL on {path}: reparse point")
    return handle


def _win_write_dacl(kernel, advapi, path: Path, dacl, *, protected: bool = False) -> None:
    """Set the DACL on a handle opened without following a reparse point."""
    advapi.SetSecurityInfo.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    advapi.SetSecurityInfo.restype = ctypes.c_uint32
    handle = _win_open_noreparse(kernel, path)
    try:
        info = 4 | (_PROTECTED_DACL if protected else 0)
        status = advapi.SetSecurityInfo(handle, 1, info, None, None, dacl, None)
        if status != 0:
            raise SandboxError(f"could not write the ACL on {path} (winerror {status})")
    finally:
        kernel.CloseHandle(handle)


def _reject_reparse_dir(path: Path) -> None:
    """After mkdir, refuse a junction that replaced the directory."""
    if sys.platform != "win32":
        return
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = _win_open_noreparse(kernel, path)
    kernel.CloseHandle(handle)


def _win_grant_traverse(lease: _WinLease, path: Path) -> None:
    """Add a non-inherited traverse ACE without walking the directory.

    Only an app-owned directory is edited. An ancestor this app does not
    own is never passed here. ``icacls`` on ``C:\\hostedtoolcache`` does
    not return, so this uses the Win32 ACL API and skips a write when
    traverse is already allowed.
    """
    _refuse_acl_write(lease, path)
    if not path.exists():
        return
    if not should_write_ace(_traverse_already_allowed(lease, path)):
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
    # A NULL DACL is replaced. Leaving it would grant Everyone, and an
    # AppContainer does not use that allow.
    null_dacl = not dacl.value
    entry = ExplicitAccess()
    # FILE_TRAVERSE | FILE_READ_ATTRIBUTES | SYNCHRONIZE
    entry.grfAccessPermissions = 0x20 | 0x80 | 0x100000
    entry.grfAccessMode = 1  # GRANT_ACCESS
    entry.grfInheritance = 0
    entry.Trustee.TrusteeForm = 0  # TRUSTEE_IS_SID
    entry.Trustee.ptstrName = lease.sid
    updated = ctypes.c_void_p()
    try:
        status = advapi.SetEntriesInAclW(
            1, ctypes.byref(entry), None if null_dacl else dacl, ctypes.byref(updated)
        )
        if status != 0:
            raise SandboxError(f"could not add a traverse ACE on {path} (winerror {status})")
        _win_write_dacl(kernel, advapi, path, updated, protected=null_dacl)
    finally:
        if security.value:
            kernel.LocalFree(security)
        if updated.value:
            kernel.LocalFree(updated)


# FILE_GENERIC_READ | FILE_GENERIC_EXECUTE, and FILE_ALL_ACCESS.
_RX_MASK = 0x1200A9
_FULL_MASK = 0x1F01FF
_READ_MASK = 0x120089
# OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE
_INHERIT_BOTH = 0x3


class _ACE_HEADER(ctypes.Structure):
    _fields_ = (
        ("AceType", ctypes.c_ubyte),
        ("AceFlags", ctypes.c_ubyte),
        ("AceSize", ctypes.c_ushort),
    )


class _ACL_HEADER(ctypes.Structure):
    _fields_ = (
        ("AclRevision", ctypes.c_ubyte),
        ("Sbz1", ctypes.c_ubyte),
        ("AclSize", ctypes.c_ushort),
        ("AceCount", ctypes.c_ushort),
        ("Sbz2", ctypes.c_ushort),
    )


def _win_bind_acl(lease: _WinLease) -> None:
    advapi = lease.advapi
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
    advapi.GetAce.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
    advapi.GetAce.restype = ctypes.c_int
    advapi.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    advapi.EqualSid.restype = ctypes.c_int
    advapi.SetEntriesInAclW.argtypes = [
        ctypes.c_uint32,
        ctypes.c_void_p,
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


def _win_ace_present(lease: _WinLease, path: Path, mask: int, ace_type: int) -> bool:
    """True when this SID already has the allow bits, or a NULL DACL allows them.

    A NULL DACL grants everyone. Replacing it with only our ACE would lock
    the user out of the file. A deny is not implied by a NULL DACL.
    """
    advapi = lease.advapi
    kernel = lease.kernel
    _win_bind_acl(lease)
    dacl = ctypes.c_void_p()
    security = ctypes.c_void_p()
    status = advapi.GetNamedSecurityInfoW(
        str(path), 1, 4, None, None, ctypes.byref(dacl), None, ctypes.byref(security)
    )
    if status != 0 or not security.value:
        return False
    try:
        if not dacl.value:
            # A NULL DACL is not this profile's ACE. The writer replaces it.
            return False
        header = _ACL_HEADER.from_address(int(dacl.value))
        for index in range(min(int(header.AceCount), 128)):
            ace = ctypes.c_void_p()
            if not advapi.GetAce(dacl, index, ctypes.byref(ace)) or not ace.value:
                continue
            info = _ACE_HEADER.from_address(int(ace.value))
            if int(info.AceType) != ace_type:
                continue
            if not advapi.EqualSid(ctypes.c_void_p(int(ace.value) + 8), lease.sid):
                continue
            if ace_type == 1:
                return True
            have = int(ctypes.c_uint32.from_address(int(ace.value) + 4).value)
            if (have & mask) == mask:
                return True
        return False
    finally:
        if security.value:
            kernel.LocalFree(security)


def _win_change_ace(lease: _WinLease, path: Path, mask: int, mode: int, inherit: int) -> None:
    """Merge one ACE. ``mode`` 1 grants and 3 denies. An existing match is left."""
    _refuse_acl_write(lease, path)
    if not should_write_ace(_win_ace_present(lease, path, mask, 0 if mode == 1 else 1)):
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

    dacl = ctypes.c_void_p()
    security = ctypes.c_void_p()
    status = advapi.GetNamedSecurityInfoW(
        str(path), 1, 4, None, None, ctypes.byref(dacl), None, ctypes.byref(security)
    )
    if status != 0:
        raise SandboxError(f"could not read the ACL on {path} (winerror {status})")
    null_dacl = not dacl.value
    entry = ExplicitAccess()
    entry.grfAccessPermissions = int(mask)
    entry.grfAccessMode = int(mode)
    entry.grfInheritance = int(inherit)
    entry.Trustee.TrusteeForm = 0
    entry.Trustee.ptstrName = lease.sid
    advapi.SetEntriesInAclW.argtypes = [
        ctypes.c_uint32,
        ctypes.POINTER(ExplicitAccess),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    updated = ctypes.c_void_p()
    try:
        status = advapi.SetEntriesInAclW(
            1, ctypes.byref(entry), None if null_dacl else dacl, ctypes.byref(updated)
        )
        if status != 0:
            raise SandboxError(f"could not edit the ACL on {path} (winerror {status})")
        _win_write_dacl(kernel, advapi, path, updated, protected=null_dacl)
    finally:
        if security.value:
            kernel.LocalFree(security)
        if updated.value:
            kernel.LocalFree(updated)


# ALL APPLICATION PACKAGES and ALL RESTRICTED APPLICATION PACKAGES.
# An AppContainer token does not receive Everyone (S-1-1-0) or BUILTIN\Users.
_PACKAGE_SID_TEXTS = ("S-1-15-2-1", "S-1-15-2-2")
_PACKAGE_SIDS: list | None = None
_FILE_READ_DATA = 0x1
_FILE_TRAVERSE_BIT = 0x20
_GENERIC_READ = 0x80000000
_GENERIC_EXECUTE = 0x20000000
_GENERIC_ALL = 0x10000000
_FILE_ALL = 0x1F01FF


def _mask_allows(have: int, *, read: bool, traverse: bool) -> bool:
    if have & _GENERIC_ALL or (have & _FILE_ALL) == _FILE_ALL:
        return True
    if traverse and not (have & _FILE_TRAVERSE_BIT or have & _GENERIC_EXECUTE):
        return False
    if read and not (have & _FILE_READ_DATA or have & _GENERIC_READ):
        return False
    return True


def ace_covers_bit(have: int, bit: int) -> bool:
    """Whether one ACE mask decides ``bit``. Generic rights cover the file bits."""
    mask = int(have)
    if mask & _GENERIC_ALL or (mask & _FILE_ALL) == _FILE_ALL:
        return True
    if bit == _FILE_TRAVERSE_BIT and mask & _GENERIC_EXECUTE:
        return True
    if bit == _FILE_READ_DATA and mask & _GENERIC_READ:
        return True
    return bool(mask & int(bit))


def _mask_bits(mask: int) -> list[int]:
    bits = []
    value = int(mask)
    bit = 1
    while value and bit <= 0x80000000:
        if value & 1:
            bits.append(bit)
        value >>= 1
        bit <<= 1
    return bits


def ace_grants_access(entries, requested: int = 0) -> bool:
    """Whether ACL order grants every bit in ``requested``.

    ``entries`` is ``(ace_type, ace_flags, mask)`` in file order. An
    inherit-only ACE does not apply to this directory. Each requested bit
    is decided on its own. A deny that overlaps any still-open bit denies
    the check, which is what Windows does with a partial deny. A later
    allow cannot restore that bit. Object allow is type 5 and object deny
    is type 6.
    """
    remaining = set(_mask_bits(int(requested or 0)))
    if not remaining:
        return False
    for ace_type, ace_flags, mask in entries:
        if int(ace_flags) & 0x08:
            continue
        covered = {bit for bit in remaining if ace_covers_bit(int(mask), bit)}
        if not covered:
            continue
        if int(ace_type) in (1, 6):
            return False
        if int(ace_type) in (0, 5):
            remaining -= covered
            if not remaining:
                return True
    return False


def _ace_sid_offset(ace_type: int, ace_address: int) -> int | None:
    """Byte offset of the SID. Object ACEs carry GUIDs before it."""
    if ace_type in (0, 1):
        return 8
    if ace_type not in (5, 6):
        return None
    flags = int(ctypes.c_uint32.from_address(ace_address + 8).value)
    offset = 12
    if flags & 0x1:
        offset += 16
    if flags & 0x2:
        offset += 16
    return offset


def _package_allow_sids(advapi) -> list:
    global _PACKAGE_SIDS
    if _PACKAGE_SIDS is not None:
        return _PACKAGE_SIDS
    advapi.ConvertStringSidToSidW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p)]
    advapi.ConvertStringSidToSidW.restype = ctypes.c_int
    found = []
    for text in _PACKAGE_SID_TEXTS:
        sid = ctypes.c_void_p()
        if advapi.ConvertStringSidToSidW(text, ctypes.byref(sid)) and sid.value:
            found.append(sid)
    _PACKAGE_SIDS = found
    return found


def _existing_package_access(path, *, read: bool, traverse: bool, extra_sid=None) -> bool:
    """True when an existing ACE already lets an AppContainer through.

    This only reads the DACL. A NULL DACL is not treated as package
    traverse: an AppContainer does not receive Everyone. A missing ACE
    is not a reason to write one on a directory this app does not own.
    """
    if sys.platform != "win32":
        return False
    try:
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    except OSError:
        return False
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
    advapi.GetAce.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
    advapi.GetAce.restype = ctypes.c_int
    advapi.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    advapi.EqualSid.restype = ctypes.c_int
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    dacl = ctypes.c_void_p()
    security = ctypes.c_void_p()
    status = advapi.GetNamedSecurityInfoW(
        str(path), 1, 4, None, None, ctypes.byref(dacl), None, ctypes.byref(security)
    )
    if status != 0 or not security.value:
        if security.value:
            kernel.LocalFree(security)
        return False
    try:
        if not dacl.value:
            return False
        sids = list(_package_allow_sids(advapi))
        if extra_sid is not None and getattr(extra_sid, "value", None):
            sids.append(extra_sid)
        if not sids:
            return False
        header = _ACL_HEADER.from_address(int(dacl.value))
        entries = []
        for index in range(min(int(header.AceCount), 256)):
            ace = ctypes.c_void_p()
            if not advapi.GetAce(dacl, index, ctypes.byref(ace)) or not ace.value:
                continue
            info = _ACE_HEADER.from_address(int(ace.value))
            ace_type = int(info.AceType)
            sid_offset = _ace_sid_offset(ace_type, int(ace.value))
            if sid_offset is None:
                continue
            ace_sid = ctypes.c_void_p(int(ace.value) + sid_offset)
            if not any(advapi.EqualSid(ace_sid, sid) for sid in sids):
                continue
            have = int(ctypes.c_uint32.from_address(int(ace.value) + 4).value)
            entries.append((ace_type, int(info.AceFlags), have))
        requested = 0
        if traverse:
            requested |= _FILE_TRAVERSE_BIT
        if read:
            requested |= _FILE_READ_DATA
        return ace_grants_access(entries, requested)
    finally:
        if security.value:
            kernel.LocalFree(security)


def _traverse_already_allowed(lease: _WinLease, path: Path) -> bool:
    mask = _FILE_TRAVERSE_BIT | 0x80 | 0x100000
    if _win_ace_present(lease, path, mask, 0):
        return True
    return _existing_package_access(path, read=False, traverse=True, extra_sid=lease.sid)


def _grant_already_present(lease: _WinLease, root: Path, mask: int) -> bool:
    """The tree was granted before, or Windows already allows the package in."""
    probes = [root]
    if root.is_dir():
        for relative in (Path("python.exe"), Path("Lib") / "os.py"):
            candidate = root / relative
            if candidate.is_file():
                probes.append(candidate)
    need_write = mask == _FULL_MASK
    for probe in probes:
        if _win_ace_present(lease, probe, mask, 0):
            continue
        if need_write or not _existing_package_access(
            probe, read=True, traverse=True, extra_sid=lease.sid
        ):
            return False
    return True


def _win_grant_tree(lease: _WinLease, root: Path, mask: int, inherit: int) -> None:
    """Propagate an allow ACE. ``icacls /T`` on the Python prefix exceeds 45s.

    The walk is in-process and stops at 180s with the directory named.
    ``__pycache__`` and distribution metadata are not read by the child.
    A file that cannot be edited is skipped, matching ``icacls /C``. The
    root itself must succeed.
    """
    deadline = time.monotonic() + 180
    pending = [root]
    seen = 0
    while pending:
        if time.monotonic() > deadline:
            raise SandboxError(f"ACL grant on {root} did not finish in 180s ({seen} entries)")
        current = pending.pop()
        try:
            is_dir = current.is_dir()
        except OSError:
            continue
        if is_dir and current != root:
            name = current.name
            if name == "__pycache__" or name.endswith(".dist-info") or name.endswith(".egg-info"):
                continue
        try:
            _win_change_ace(lease, current, mask, 1, inherit if is_dir else 0)
        except SandboxError as exc:
            if current == root or "refusing to edit the ACL" in str(exc):
                raise
            continue
        seen += 1
        if not is_dir:
            continue
        try:
            pending.extend(current.iterdir())
        except OSError:
            continue


def _win_revoke_ace(lease: _WinLease, path: Path) -> None:
    """Drop this profile's ACEs from one file. Does not walk children."""
    if acl_revoke_problem(path, environ=os.environ):
        return
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

    _win_bind_acl(lease)
    dacl = ctypes.c_void_p()
    security = ctypes.c_void_p()
    status = advapi.GetNamedSecurityInfoW(
        str(path), 1, 4, None, None, ctypes.byref(dacl), None, ctypes.byref(security)
    )
    if status != 0 or not dacl.value:
        if security.value:
            kernel.LocalFree(security)
        return
    entry = ExplicitAccess()
    entry.grfAccessMode = 4  # REVOKE_ACCESS
    entry.Trustee.TrusteeForm = 0
    entry.Trustee.ptstrName = lease.sid
    advapi.SetEntriesInAclW.argtypes = [
        ctypes.c_uint32,
        ctypes.POINTER(ExplicitAccess),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    updated = ctypes.c_void_p()
    try:
        status = advapi.SetEntriesInAclW(1, ctypes.byref(entry), dacl, ctypes.byref(updated))
        if status != 0:
            raise SandboxError(f"could not revoke the ACL on {path} (winerror {status})")
        _win_write_dacl(kernel, advapi, path, updated, protected=False)
    finally:
        if security.value:
            kernel.LocalFree(security)
        if updated.value:
            kernel.LocalFree(updated)


def _win_revoke_tree(lease: _WinLease, root: Path) -> None:
    """Revoke one tree. ``icacls`` on ``C:\\hostedtoolcache`` does not return."""
    if acl_revoke_problem(root, environ=os.environ):
        return
    deadline = time.monotonic() + 180
    pending = [root]
    while pending:
        if time.monotonic() > deadline:
            return
        current = pending.pop()
        try:
            is_dir = current.is_dir()
        except OSError:
            continue
        if is_dir and current != root:
            name = current.name
            if name == "__pycache__" or name.endswith(".dist-info") or name.endswith(".egg-info"):
                continue
        try:
            _win_revoke_ace(lease, current)
        except SandboxError:
            if current == root:
                raise
        if not is_dir:
            continue
        try:
            pending.extend(current.iterdir())
        except OSError:
            continue


def _win_grant(lease: _WinLease, path: Path, rights: str, *, recurse: bool) -> None:
    _refuse_acl_write(lease, path)
    if not path.exists():
        return
    if not recurse and rights == "(X)":
        _win_grant_traverse(lease, path)
        return
    if rights == "(OI)(CI)(RX)":
        mask, inherit = _RX_MASK, _INHERIT_BOTH
    elif rights == "(OI)(CI)(F)":
        mask, inherit = _FULL_MASK, _INHERIT_BOTH
    else:
        raise SandboxError(f"unsupported grant {rights}")
    note_phase(f"grant-start {path.name}")
    if recurse and _grant_already_present(lease, path, mask):
        note_phase(f"grant-skip {path.name}")
        return
    if recurse:
        _win_grant_tree(lease, path, mask, inherit)
    else:
        _win_change_ace(lease, path, mask, 1, inherit if path.is_dir() else 0)
    note_phase(f"grant-done {path.name}")


def _win_deny(lease: _WinLease, path: Path) -> None:
    _refuse_acl_write(lease, path)
    command = [_icacls(), str(path), "/deny", f"*{lease.sid_text}:(OI)(CI)(F)", "/T", "/C", "/Q"]
    _icacls_run(command)


def _win_deny_shallow(lease: _WinLease, path: Path) -> None:
    """Deny reading one app-owned directory. Children keep the ACEs they have.

    The ACE is skipped when it is already present. A directory this app
    does not own is not edited: the AppContainer cannot read it anyway.
    """
    _refuse_acl_write(lease, path)
    if not path.exists():
        return
    if _win_ace_present(lease, path, _READ_MASK, 1):
        return
    _win_change_ace(lease, path, _READ_MASK, 3, 0)


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
    """Drop ACEs this grant added. Never calls ``icacls``.

    ``icacls`` without ``/T`` on ``C:\\hostedtoolcache`` still does not
    return. A failed grant used to spend 45 seconds there and every test
    then reported that timeout instead of the grant error.
    """
    if lease is None or not lease.sid_text:
        return
    for path in paths:
        if not path or not Path(path).exists():
            continue
        target = Path(path)
        if recurse and target.is_dir():
            _win_revoke_tree(lease, target)
        else:
            _win_revoke_ace(lease, target)


def _win_reject_overlap(grants, denied) -> None:
    venv = _interpreter_venv()
    for banned in denied:
        if banned is None:
            continue
        banned_path = Path(banned)
        for grant, _recurse in grants:
            message = runtime_overlap_error(grant, banned_path, venv=venv)
            if message is not None:
                raise SandboxError(message)


def _win_grant_work(lease: _WinLease, work: Path) -> list[tuple[Path, bool]]:
    """Grant the private directory. Ancestors this app did not create are read, not written."""
    allow = [str(path) for path, _flag in lease.grant_paths]
    allow.extend(str(path) for path in _CREATED_APP_DIRS)
    allow.append(str(work))
    lease.acl_allow = _dedupe_acl(allow)
    # A private directory under the AppContainer folder is reachable because
    # that folder is the profile's own directory. Its ancestors, including
    # the user-profile root and Temp, are not granted and are not required
    # to show a package ACE.
    if not _trusted_runtime_root(work):
        _require_existing_ancestor_traverse(work, lease.acl_allow)
    for parent in acl_ancestor_paths(str(work)):
        if not any(acl_same(parent, root) for root in lease.acl_allow):
            continue
        parent_path = Path(parent)
        if any(acl_same(parent_path, earlier) for earlier in lease.ancestor_grants):
            continue
        try:
            if not parent_path.exists():
                continue
        except OSError:
            continue
        _win_grant(lease, parent_path, "(X)", recurse=False)
        lease.ancestor_grants.add(parent_path)
    _win_grant(lease, work, "(OI)(CI)(F)", recurse=True)
    return [(work, True)]


def _win_apply_denies(lease: _WinLease, denied) -> list[Path]:
    applied = []
    for banned in denied:
        if banned is None:
            continue
        path = Path(banned)
        if not path.exists():
            continue
        try:
            _refuse_acl_write(lease, path)
        except SandboxError:
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

    _ensure_runtime_integrity(lease)
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
        executable = _child_executable()
        command = subprocess.list2cmdline(_interpreter_command(work / "sandbox_child.py", child=True))
        command_buf = ctypes.create_unicode_buffer(command)
        env_buf = _windows_env_block(_windows_env(work))
        created = _WinProcessInformation()
        # CREATE_SUSPENDED | EXTENDED_STARTUPINFO_PRESENT | CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT
        flags = 0x00000004 | 0x00080000 | 0x08000000 | 0x00000400
        # CREATE_BREAKAWAY_FROM_JOB. The worker job's commit ceiling must
        # not charge this child: that wait is the empty result pipe. If the
        # outer job forbids breakaway, start in the worker job instead.
        # That is not a retry of a missing executable. A second pass is only
        # for a start the runner refused while the executable was present:
        # a busy GitHub worker has failed the first AppContainer create and
        # accepted the same call a moment later. Winerror 2 is file-not-found
        # and 193 is not a valid application; those do not get the second pass.
        ok = False
        failed = 0
        passes = ((0x01000000, "breakaway"), (0, "in-job"))
        for pass_index in (1, 2):
            for extra, label in passes:
                prefix = "createprocess" if pass_index == 1 else "createprocess-retry"
                note_phase(f"{prefix}-{label}")
                ok = kernel.CreateProcessW(
                    executable,
                    command_buf,
                    None,
                    None,
                    1,
                    flags | extra,
                    ctypes.cast(env_buf, ctypes.c_void_p),
                    str(work),
                    ctypes.byref(info),
                    ctypes.byref(created),
                )
                if ok:
                    note_phase(f"createprocess-ok-{label}")
                    break
                failed = ctypes.get_last_error()
                note_phase(f"createprocess-fail-{label}-{failed}")
                if extra == 0 or failed != 5:
                    break
            if ok or failed in (2, 193):
                break
            time.sleep(0.25)
        if not ok:
            code = failed
            executable_path = Path(executable)
            raise SandboxError(
                "CreateProcessW failed (winerror %s); executable %s exists=%s cwd=%s cwd_exists=%s"
                % (code, executable_path, executable_path.is_file(), work, work.is_dir())
            )
        process = created.hProcess
        thread = created.hThread
        note_phase("token-check")
        _win_require_container(lease, process)
        note_phase("job-arm")
        job = _win_arm_job(kernel, process, memory_bytes=memory_bytes, cpu_seconds=cpu_seconds)
        note_phase("resume")
        if kernel.ResumeThread(thread) == 0xFFFFFFFF:
            raise _win_fail("ResumeThread failed")
        note_phase("resumed")
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
    parts = [os.path.dirname(_child_executable())]
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
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
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
