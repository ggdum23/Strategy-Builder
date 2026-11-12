"""Return 0.0 when clone3 stops at the process ceiling, else 1.0.

fork is already denied. clone3 with no flags still creates a process, so
the ceiling is RLIMIT_NPROC. The loop stops at the first EAGAIN and reaps
every child it started.
"""

import ctypes
import errno
import os


class _CloneArgs(ctypes.Structure):
    _fields_ = (
        ("flags", ctypes.c_uint64),
        ("pidfd", ctypes.c_uint64),
        ("child_tid", ctypes.c_uint64),
        ("parent_tid", ctypes.c_uint64),
        ("exit_signal", ctypes.c_uint64),
        ("stack", ctypes.c_uint64),
        ("stack_size", ctypes.c_uint64),
        ("tls", ctypes.c_uint64),
    )


class Strategy:
    def on_bar(self, bar, state):
        libc = ctypes.CDLL(None, use_errno=True)
        libc.syscall.restype = ctypes.c_long
        kids = []
        capped = False
        for _ in range(80):
            args = _CloneArgs()
            args.exit_signal = 17
            pid = libc.syscall(435, ctypes.byref(args), ctypes.c_size_t(ctypes.sizeof(args)))
            if pid == 0:
                os._exit(0)
            if pid > 0:
                kids.append(int(pid))
                continue
            if ctypes.get_errno() == errno.EAGAIN:
                capped = True
                break
            capped = False
            break
        for child in kids:
            try:
                os.waitpid(child, 0)
            except OSError:
                pass
        if capped and len(kids) <= 64:
            return 0.0
        return 1.0
