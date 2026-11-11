"""Parent-side memory watches for a strategy process.

The cap is the worker's resident set, including children the strategy starts.
It is not a fee, a fill rule, or a sandbox. Phase 4 still owns the OS jail.
On Windows every worker is placed in a job object before it is allowed to run
the strategy. The job has ``KILL_ON_JOB_CLOSE`` so the worker and anything it
spawned die together. A memory cap, when one was requested, is a higher
commit-charge ceiling than the resident set, so one allocation cannot exhaust
the machine between polls.
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path


def process_rss_bytes(pid: int) -> int | None:
    """Resident set of one process, or None when it cannot be read."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if sys.platform == "linux":
        return _linux_rss(pid)
    if sys.platform == "win32":
        return _windows_rss(pid)
    return None


def process_tree_rss_bytes(pid: int) -> int | None:
    """Resident set of ``pid`` plus the processes it has started."""
    own = process_rss_bytes(pid)
    if own is None:
        return None
    total = own
    seen = {pid}
    stack = _child_pids(pid)
    while stack:
        child = stack.pop()
        if child in seen:
            continue
        seen.add(child)
        child_rss = process_rss_bytes(child)
        if child_rss is not None:
            total += child_rss
        stack.extend(_child_pids(child))
    return total


def current_rss() -> int:
    value = process_rss_bytes(_own_pid())
    if value is None:
        raise RuntimeError("resident set is not available on this platform")
    return value


def limit_process_memory(pid: int, memory_bytes: int):
    """Hard ceiling for a Windows worker. Linux polling enforces the same cap.

    Returns an object with ``close()`` and ``terminate()``, or None when this
    platform cannot arm a job object. A failure to arm the job does not raise:
    the parent's resident-set poll is still the cap the runner reports.
    """
    if not isinstance(memory_bytes, int) or isinstance(memory_bytes, bool) or memory_bytes <= 0:
        raise ValueError("memory cap must be a positive number of bytes")
    return arm_worker_job(pid, memory_bytes)


def arm_worker_job(pid: int, memory_bytes: int | None = None):
    """Put a Windows worker in a job that dies with every process it starts.

    The job is armed for every worker, including a run with no memory cap.
    ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` is set so closing the last handle
    kills the worker and the processes it spawned. ``terminate`` does that
    immediately. A memory cap, when one was requested, is the same commit-charge
    ceiling as before, added on top of that flag.

    Linux returns None. The runner kills the process group there. A failure to
    arm the job returns None and does not raise.
    """
    if sys.platform != "win32":
        return None
    if memory_bytes is not None and (
        not isinstance(memory_bytes, int) or isinstance(memory_bytes, bool) or memory_bytes <= 0
    ):
        raise ValueError("memory cap must be a positive number of bytes")
    try:
        return _WinJob.arm(pid, memory_bytes)
    except OSError:
        return None


def _own_pid() -> int:
    import os

    return os.getpid()


def _linux_rss(pid: int) -> int | None:
    try:
        text = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("VmRSS:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1]) * 1024
    return None


def _child_pids(pid: int) -> list[int]:
    if sys.platform != "linux":
        return []
    path = Path(f"/proc/{pid}/task/{pid}/children")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return [int(part) for part in text.split() if part.isdigit()]


def _windows_rss(pid: int) -> int | None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    # PROCESS_QUERY_LIMITED_INFORMATION
    handle = kernel32.OpenProcess(0x1000, 0, pid)
    if not handle:
        return None
    try:
        counters = _PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(counters)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32]
        psapi.GetProcessMemoryInfo.restype = ctypes.c_int
        ok = psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
        if not ok:
            return None
        return int(counters.WorkingSetSize)
    finally:
        kernel32.CloseHandle(handle)


class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("PageFaultCount", ctypes.c_uint32),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _WinJob:
    """A job object held open until the worker is finished."""

    def __init__(self, kernel, handle):
        self._kernel = kernel
        self._handle = handle

    @classmethod
    def arm(cls, pid: int, memory_bytes: int | None):
        # Win64 layout. A different size means the fields would be misread.
        if ctypes.sizeof(_JOBOBJECT_EXTENDED_LIMIT_INFORMATION) != 144:
            return None
        if ctypes.sizeof(_PROCESS_MEMORY_COUNTERS) != 72:
            return None
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        kernel32.SetInformationJobObject.restype = ctypes.c_int
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel32.AssignProcessToJobObject.restype = ctypes.c_int
        kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.TerminateJobObject.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        # PROCESS_SET_QUOTA | PROCESS_TERMINATE, required to assign a job.
        process = kernel32.OpenProcess(0x0100 | 0x0001, 0, pid)
        if not process:
            kernel32.CloseHandle(job)
            return None
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        # KILL_ON_JOB_CLOSE is on every worker. A memory cap, when the caller
        # asked for one, is a commit-charge ceiling above the resident-set poll.
        flags = 0x2000
        if memory_bytes is not None:
            ceiling = int(memory_bytes) * 4 + 256 * 1024 * 1024
            info.ProcessMemoryLimit = ceiling
            info.JobMemoryLimit = ceiling
            # JOB_OBJECT_LIMIT_PROCESS_MEMORY | JOB_OBJECT_LIMIT_JOB_MEMORY
            flags |= 0x00000100 | 0x00000200
        info.BasicLimitInformation.LimitFlags = flags
        # JobObjectExtendedLimitInformation = 9
        armed = kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
        assigned = armed and kernel32.AssignProcessToJobObject(job, process)
        kernel32.CloseHandle(process)
        if not assigned:
            kernel32.CloseHandle(job)
            return None
        return cls(kernel32, job)

    def terminate(self) -> None:
        """Kill the worker and every process it started."""
        handle = self._handle
        if not handle:
            return
        self._kernel.TerminateJobObject(handle, 1)

    def close(self) -> None:
        handle = self._handle
        self._handle = None
        if handle:
            self._kernel.CloseHandle(handle)
