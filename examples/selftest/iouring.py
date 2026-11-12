"""Return 0.0 when io_uring_setup is denied, else 1.0.

The numbers are the generic and x86_64 values. A machine with a different
number reports itself as not denied, so the probe fails instead of guessing.
"""

import ctypes
import errno
import os


_NR_IO_URING_SETUP = {
    "x86_64": 425,
    "amd64": 425,
    "aarch64": 425,
    "arm64": 425,
}


class Strategy:
    def on_bar(self, bar, state):
        number = _NR_IO_URING_SETUP.get(os.uname().machine.lower())
        if number is None:
            return 1.0
        libc = ctypes.CDLL(None, use_errno=True)
        libc.syscall.restype = ctypes.c_long
        params = ctypes.create_string_buffer(256)
        rc = libc.syscall(number, ctypes.c_uint(8), ctypes.cast(params, ctypes.c_void_p))
        if rc >= 0:
            os.close(int(rc))
            return 1.0
        return 0.0 if ctypes.get_errno() == errno.EPERM else 1.0
