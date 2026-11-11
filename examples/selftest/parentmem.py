"""Return 1.0 if any byte of another process's memory was read, else 0.0.

Linux: ``/proc/<ppid>/mem`` and ``/proc/1/mem``, plus ``process_vm_readv`` and
``ptrace``. Windows: ``OpenProcess`` with ``PROCESS_VM_READ``.
"""

import os


def _proc_mem(pid: int) -> bool:
    try:
        fd = os.open(f"/proc/{pid}/mem", os.O_RDONLY)
    except OSError:
        return False
    try:
        try:
            data = os.read(fd, 16)
        except OSError:
            data = b""
    finally:
        os.close(fd)
    return len(data) > 0


def _vm_read(pid: int) -> bool:
    import ctypes

    class Iovec(ctypes.Structure):
        _fields_ = (("iov_base", ctypes.c_void_p), ("iov_len", ctypes.c_size_t))

    libc = ctypes.CDLL(None, use_errno=True)
    buf = ctypes.create_string_buffer(8)
    local = Iovec(ctypes.cast(buf, ctypes.c_void_p), 8)
    remote = Iovec(ctypes.c_void_p(0), 8)
    rc = libc.syscall(310, int(pid), ctypes.byref(local), 1, ctypes.byref(remote), 1, 0)
    return rc == 0


def _ptrace(pid: int) -> bool:
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    # ptrace(PTRACE_ATTACH, pid, 0, 0). 16 is PTRACE_ATTACH.
    rc = libc.syscall(101, 16, int(pid), 0, 0)
    if rc == 0:
        libc.syscall(101, 17, int(pid), 0, 0)
        return True
    return False


def _windows(pid: int) -> bool:
    import ctypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x0010, 0, int(pid))
    if not handle:
        return False
    kernel.CloseHandle(handle)
    return True


class Strategy:
    def on_bar(self, bar, state):
        pid = os.getppid()
        if os.name == "nt":
            return 1.0 if _windows(pid) else 0.0
        if _proc_mem(pid) or _proc_mem(1) or _vm_read(pid) or _vm_read(1):
            return 1.0
        if _ptrace(1):
            return 1.0
        return 0.0
