"""Network probe. Both connects must fail with an access error.

The loopback target is a server the test is already listening on. A closed
port can answer ECONNREFUSED before the jail is asked, so that result is not
a block. A timeout or a completed handshake is not a block either. On
Windows the process also has to be an AppContainer with no capabilities.
"""

import errno
import json
import sys

_LOOPBACK = ("127.0.0.1", 44123)
_EXTERNAL = ("1.1.1.1", 443)
_BLOCKED = {errno.EPERM, errno.EACCES, 10013}


def _token_ok() -> bool:
    if sys.platform != "win32":
        return True
    import ctypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
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
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    facts = {"is_app_container": 0, "capability_count": -1}
    token = ctypes.c_void_p()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        sys.stderr.write("token open failed\n")
        return False
    try:
        flag = ctypes.c_uint32()
        needed = ctypes.c_uint32()
        if not advapi.GetTokenInformation(token, 29, ctypes.byref(flag), 4, ctypes.byref(needed)):
            sys.stderr.write("token query failed\n")
            return False
        facts["is_app_container"] = int(flag.value)
        advapi.GetTokenInformation(token, 30, None, 0, ctypes.byref(needed))
        size = int(needed.value or 4)
        buf = ctypes.create_string_buffer(size)
        if not advapi.GetTokenInformation(token, 30, buf, size, ctypes.byref(needed)):
            sys.stderr.write("token capabilities failed\n")
            return False
        facts["capability_count"] = int(ctypes.c_uint32.from_buffer_copy(buf).value)
    finally:
        kernel.CloseHandle(token)
    sys.stderr.write("token " + json.dumps(facts) + "\n")
    return facts["is_app_container"] == 1 and facts["capability_count"] == 0


def _blocked(exc: OSError) -> bool:
    if exc.errno in _BLOCKED:
        return True
    return getattr(exc, "winerror", None) in _BLOCKED


def _access_denied(host, port) -> bool:
    import socket

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    except OSError as exc:
        return _blocked(exc)
    try:
        sock.settimeout(3)
        sock.connect((host, port))
    except OSError as exc:
        return _blocked(exc)
    except Exception:
        return False
    else:
        return False
    finally:
        sock.close()


class Strategy:
    def on_bar(self, _bar, _state):
        if not _token_ok():
            return 1.0
        loopback = _access_denied(*_LOOPBACK)
        external = _access_denied(*_EXTERNAL)
        if loopback and external:
            return 0.0
        return 1.0
