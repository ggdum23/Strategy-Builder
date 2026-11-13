"""Parent-side memory watches for a strategy process.

The cap is the worker's resident set, including children the strategy starts.
It is not a fee, a fill rule, or a sandbox. Phase 4 still owns the OS jail.
On Windows every worker is placed in a job object before it is allowed to run
the strategy. The job has ``KILL_ON_JOB_CLOSE`` so the worker and anything it
spawned die together. It does not set a process or job commit limit: on the
GitHub runner that limit makes ``CreateProcess`` of an AppContainer child
wait for the whole parent budget, and the result pipe stays empty. A higher
ceiling did not stop the wait. The resident-set poll is the cap that is
reported. ``BREAKAWAY_OK`` is set when the nested job accepts it, so the
jailed child can leave this job; the sandbox job still bounds that child.
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


def process_commit_bytes(pid: int) -> int | None:
    """Commit charge of one process, or None when it cannot be read."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if sys.platform != "win32":
        return None
    return _windows_memory_field(pid, "PagefileUsage")


def process_tree_detail(pid: int) -> str:
    """One line: this process and the children toolhelp can see."""
    parts = [f"worker_rss={process_rss_bytes(pid)}", f"worker_commit={process_commit_bytes(pid)}"]
    shown = []
    for child in _child_pids(pid)[:8]:
        shown.append(f"{child}:rss={process_rss_bytes(child)}:commit={process_commit_bytes(child)}")
    parts.append("children=" + (",".join(shown) if shown else "none"))
    return " ".join(parts)


def _valid_pid(pid) -> bool:
    return isinstance(pid, int) and not isinstance(pid, bool) and pid > 0


def sum_process_tree(root: int, extra_pids, read_one, children_of) -> int | None:
    """Sum ``read_one`` over ``root``, its descendants, and ``extra_pids``.

    Each extra pid is a root of its own. Its children are walked too. A pid
    that cannot be read is skipped. A pid already counted is not added twice.
    """
    if not _valid_pid(root):
        return None
    own = read_one(root)
    if own is None:
        return None
    total = int(own)
    seen = {int(root)}
    stack = list(children_of(root) or [])
    for extra in extra_pids or ():
        if _valid_pid(extra):
            stack.append(int(extra))
    while stack:
        child = stack.pop()
        if not _valid_pid(child) or child in seen:
            continue
        seen.add(child)
        value = read_one(child)
        if value is not None:
            total += int(value)
        stack.extend(children_of(child) or [])
    return total


def process_tree_rss_bytes(pid: int, extra_pids=()) -> int | None:
    """Resident set of ``pid``, the processes it has started, and ``extra_pids``.

    ``extra_pids`` are jailed children toolhelp did not list (an AppContainer
    can be missing there). Their children are included. A pid already in the
    tree is not added twice.
    """
    return sum_process_tree(pid, extra_pids, process_rss_bytes, _child_pids)


def process_tree_commit_bytes(pid: int, extra_pids=()) -> int | None:
    """Commit charge of the same tree. Windows only; other platforms return None."""
    if sys.platform != "win32":
        return None
    return sum_process_tree(pid, extra_pids, process_commit_bytes, _child_pids)


def process_tree_watch_bytes(pid: int, extra_pids=()) -> int | None:
    """The larger of resident set and, on Windows, commit charge.

    A dirty allocation that has not been faulted into the working set is
    still commit. The poll reports whichever number is higher.
    """
    rss = process_tree_rss_bytes(pid, extra_pids)
    commit = process_tree_commit_bytes(pid, extra_pids)
    if rss is None:
        return commit
    if commit is None:
        return rss
    return max(rss, commit)


def member_list_overflow(assigned: int, cap: int = 32) -> str | None:
    """Fail the book when the job reports more members than the list can hold."""
    if isinstance(assigned, bool) or not isinstance(assigned, int):
        return None
    if assigned > int(cap):
        return (
            f"walk-forward worker job has {assigned} processes; "
            f"refusing to sum a {int(cap)}-pid prefix"
        )
    return None


def cgroup_memory_available(controllers: str) -> bool:
    """True when this cgroup v2 mount lists the memory controller."""
    return "memory" in str(controllers or "").split()


def apply_cgroup_memory_max(pid: int, memory_bytes: int) -> bool:
    """Put ``pid`` in a cgroup with ``memory.max`` when the controller is mounted.

    A missing controller or a write the user cannot make returns False. The
    rlimit and the parent poll still apply. The directory is
    ``/sys/fs/cgroup/strategybuilder-<pid>``.
    """
    if sys.platform != "linux" or not _valid_pid(pid):
        return False
    if not isinstance(memory_bytes, int) or isinstance(memory_bytes, bool) or memory_bytes <= 0:
        return False
    root = Path("/sys/fs/cgroup")
    controllers = root / "cgroup.controllers"
    try:
        text = controllers.read_text(encoding="utf-8")
    except OSError:
        return False
    if not cgroup_memory_available(text):
        return False
    group = root / f"strategybuilder-{int(pid)}"
    members = [int(pid)]
    pending = [int(pid)]
    while pending:
        current = pending.pop()
        for child in _child_pids(current):
            if child not in members:
                members.append(child)
                pending.append(child)
    try:
        group.mkdir(exist_ok=True)
        (group / "memory.max").write_text(str(int(memory_bytes)), encoding="utf-8")
        (group / "cgroup.procs").write_text(str(members[0]), encoding="utf-8")
        for child in members[1:]:
            try:
                (group / "cgroup.procs").write_text(str(child), encoding="utf-8")
            except OSError:
                continue
    except OSError:
        try:
            group.rmdir()
        except OSError:
            pass
        return False
    return True


def release_cgroup_memory(pid: int) -> None:
    """Remove the cgroup created for ``pid``. A live member makes rmdir fail."""
    if sys.platform != "linux" or not _valid_pid(pid):
        return
    group = Path("/sys/fs/cgroup") / f"strategybuilder-{int(pid)}"
    try:
        group.rmdir()
    except OSError:
        return


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
    arm the job returns None and does not raise. On Windows the runner treats
    that None as a failed book: it does not continue with pid-only signaling.
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
    if sys.platform == "linux":
        path = Path(f"/proc/{pid}/task/{pid}/children")
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return []
        return [int(part) for part in text.split() if part.isdigit()]
    if sys.platform == "win32":
        return _windows_child_pids(pid)
    return []


def _windows_child_pids(pid: int) -> list[int]:
    """Direct children, so a memory cap includes the jailed strategy."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    kernel.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel.Process32FirstW.restype = ctypes.c_int
    kernel.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel.Process32NextW.restype = ctypes.c_int
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int

    class _ProcessEntry(ctypes.Structure):
        _fields_ = (
            ("dwSize", ctypes.c_uint32),
            ("cntUsage", ctypes.c_uint32),
            ("th32ProcessID", ctypes.c_uint32),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", ctypes.c_uint32),
            ("cntThreads", ctypes.c_uint32),
            ("th32ParentProcessID", ctypes.c_uint32),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", ctypes.c_uint32),
            ("szExeFile", ctypes.c_wchar * 260),
        )

    snapshot = kernel.CreateToolhelp32Snapshot(0x00000002, 0)
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        return []
    found = []
    try:
        entry = _ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        ok = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            if int(entry.th32ParentProcessID) == int(pid):
                found.append(int(entry.th32ProcessID))
            ok = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    return found


def _windows_rss(pid: int) -> int | None:
    return _windows_memory_field(pid, "WorkingSetSize")


def _windows_memory_field(pid: int, field: str) -> int | None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    # Limited information first. An AppContainer child can refuse that and
    # still answer QUERY_INFORMATION | VM_READ from the process that created it.
    handle = None
    for access in (0x1000, 0x0410):
        handle = kernel32.OpenProcess(access, 0, pid)
        if handle:
            break
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
        return int(getattr(counters, field))
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
    def arm(cls, pid: int, _memory_bytes: int | None):
        # ``_memory_bytes`` is the cap the parent poll enforces. It is not a
        # job commit limit: that limit made CreateProcess of the AppContainer
        # child wait on the runner, even when the ceiling was gigabytes above
        # the working set. The result pipe stayed empty and the sibling
        # blocked on the profile mutex.
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

        # PROCESS_SET_QUOTA | PROCESS_TERMINATE, required to assign a job.
        process = kernel32.OpenProcess(0x0100 | 0x0001, 0, pid)
        if not process:
            return None
        # KILL_ON_JOB_CLOSE, then the same flag with BREAKAWAY_OK. A nested
        # job rejects breakaway; the child then stays in this job and dies
        # with it. Either way there is no commit ceiling.
        job = None
        for flags in (0x2000 | 0x0800, 0x2000):
            candidate = kernel32.CreateJobObjectW(None, None)
            if not candidate:
                continue
            info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = flags
            # JobObjectExtendedLimitInformation = 9
            armed = kernel32.SetInformationJobObject(
                candidate, 9, ctypes.byref(info), ctypes.sizeof(info)
            )
            assigned = armed and kernel32.AssignProcessToJobObject(candidate, process)
            if assigned:
                job = candidate
                break
            kernel32.CloseHandle(candidate)
        kernel32.CloseHandle(process)
        if not job:
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

    def _query(self, kind: int, info) -> bool:
        kernel = self._kernel
        kernel.QueryInformationJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        kernel.QueryInformationJobObject.restype = ctypes.c_int
        return bool(
            kernel.QueryInformationJobObject(self._handle, kind, ctypes.byref(info), ctypes.sizeof(info), None)
        )

    def describe(self) -> str:
        """Limit flags and the job's peak commit, for a hang log."""
        if not self._handle:
            return "job=closed"
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        if not self._query(9, info):
            return f"job_query_failed={ctypes.get_last_error()}"
        return (
            "job_flags=0x%x process_limit=%s job_limit=%s peak_job=%s peak_process=%s"
            % (
                int(info.BasicLimitInformation.LimitFlags),
                int(info.ProcessMemoryLimit),
                int(info.JobMemoryLimit),
                int(info.PeakJobMemoryUsed),
                int(info.PeakProcessMemoryUsed),
            )
        )

    def member_pids(self) -> list[int]:
        found = self._member_pids()
        return list(found or [])

    def member_rss(self) -> int | None:
        """Working set of every process still in this job.

        Toolhelp follows parent pids. An AppContainer child can be missing
        there and still be in the job when it did not break away. The larger
        of the two sums is the cap the parent enforces.
        """
        pids = self._member_pids()
        if not pids:
            return None
        total = 0
        seen = False
        for pid in pids:
            rss = process_rss_bytes(int(pid))
            if rss is None:
                continue
            seen = True
            total += rss
        return total if seen else None

    def _member_pids(self) -> list[int] | None:
        class _List(ctypes.Structure):
            _fields_ = (
                ("NumberOfAssignedProcesses", ctypes.c_uint32),
                ("NumberOfProcessIdsInList", ctypes.c_uint32),
                ("ProcessIdList", ctypes.c_size_t * 32),
            )

        info = _List()
        if not self._handle or not self._query(3, info):
            return None
        count = min(int(info.NumberOfProcessIdsInList), 32)
        return [int(info.ProcessIdList[index]) for index in range(count)]

    def assigned_process_count(self) -> int | None:
        """How many processes the job says it holds, even past the 32-pid list."""

        class _List(ctypes.Structure):
            _fields_ = (
                ("NumberOfAssignedProcesses", ctypes.c_uint32),
                ("NumberOfProcessIdsInList", ctypes.c_uint32),
                ("ProcessIdList", ctypes.c_size_t * 32),
            )

        info = _List()
        if not self._handle or not self._query(3, info):
            return None
        return int(info.NumberOfAssignedProcesses)
