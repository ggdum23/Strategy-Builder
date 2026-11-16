import ctypes
import subprocess
import sys

use_shell_execute = False


def wait_gui_process(file: str, arguments: str) -> int:
    if use_shell_execute:
        raise RuntimeError(f"could not start {file}")
    command = subprocess.list2cmdline([file]) + " " + arguments
    try:
        completed = subprocess.run(command, shell=False, check=False)
    except OSError as exc:
        raise RuntimeError(f"could not start {file}") from exc
    if completed.returncode is None:
        raise RuntimeError(f"could not start {file}")
    return int(completed.returncode)


def _process_names() -> list[str]:
    if sys.platform != "win32":
        return []
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    kernel.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel.Process32FirstW.restype = ctypes.c_int
    kernel.Process32NextW.restype = ctypes.c_int
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    class PROCESSENTRY32W(ctypes.Structure):
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

    entry = PROCESSENTRY32W
    snapshot = kernel.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot in (None, 0, ctypes.c_void_p(-1).value):
        return []
    kernel.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(entry)]
    kernel.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(entry)]
    names: list[str] = []
    row = entry()
    row.dwSize = ctypes.sizeof(row)
    try:
        if not kernel.Process32FirstW(snapshot, ctypes.byref(row)):
            return []
        while True:
            names.append(row.szExeFile)
            if not kernel.Process32NextW(snapshot, ctypes.byref(row)):
                break
    finally:
        kernel.CloseHandle(snapshot)
    return names


def uninstall_phase_running() -> bool:
    for name in _process_names():
        folded = name.casefold()
        if folded.startswith("unins") or folded in {"_unins.tmp", "_unins"}:
            return True
        if folded.endswith("\\_unins.tmp") or "\\unins" in folded:
            return True
    return False
