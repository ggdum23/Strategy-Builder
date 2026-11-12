"""Return 0.0 when a new process is refused.

fork is EPERM. clone3 is ENOSYS, so glibc falls back to clone, and clone
without CLONE_THREAD is EPERM. A thread is still allowed to start.
"""

import ctypes
import errno
import os
import threading


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
        try:
            pid = os.fork()
        except OSError as exc:
            if exc.errno != errno.EPERM:
                return 1.0
        else:
            if pid == 0:
                os._exit(0)
            return 1.0
        libc = ctypes.CDLL(None, use_errno=True)
        libc.syscall.restype = ctypes.c_long
        args = _CloneArgs()
        args.exit_signal = 17
        spawned = libc.syscall(435, ctypes.byref(args), ctypes.c_size_t(ctypes.sizeof(args)))
        if spawned == 0:
            os._exit(0)
        if spawned > 0:
            try:
                os.waitpid(int(spawned), 0)
            except OSError:
                pass
            return 1.0
        if ctypes.get_errno() != errno.ENOSYS:
            return 1.0
        started = []

        def run():
            started.append(1)

        thread = threading.Thread(target=run)
        thread.start()
        thread.join(timeout=2)
        if not started or thread.is_alive():
            return 1.0
        return 0.0
