"""Return 0.0 when io_uring setup, enter, and register are all EPERM.

The numbers are the generic and x86_64 values. A machine with a different
number reports itself as not denied, so the probe fails instead of guessing.
"""

import ctypes
import errno
import os


_NUMBERS = (425, 426, 427)


class Strategy:
    def on_bar(self, bar, state):
        machine = os.uname().machine.lower()
        if machine not in {"x86_64", "amd64", "aarch64", "arm64"}:
            return 1.0
        libc = ctypes.CDLL(None, use_errno=True)
        libc.syscall.restype = ctypes.c_long
        params = ctypes.create_string_buffer(256)
        calls = (
            (425, (ctypes.c_uint(8), ctypes.cast(params, ctypes.c_void_p))),
            (
                426,
                (
                    ctypes.c_uint(0),
                    ctypes.c_uint(0),
                    ctypes.c_uint(0),
                    ctypes.c_uint(0),
                    ctypes.c_void_p(0),
                ),
            ),
            (
                427,
                (
                    ctypes.c_uint(0),
                    ctypes.c_uint(0),
                    ctypes.c_void_p(0),
                    ctypes.c_uint(0),
                ),
            ),
        )
        for number, args in calls:
            if number not in _NUMBERS:
                return 1.0
            rc = libc.syscall(number, *args)
            if rc >= 0:
                try:
                    os.close(int(rc))
                except OSError:
                    pass
                return 1.0
            if ctypes.get_errno() != errno.EPERM:
                return 1.0
        return 0.0
