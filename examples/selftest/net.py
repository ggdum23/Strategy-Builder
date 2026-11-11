"""Return 0.0 when a socket is refused by the jail, else 1.0.

ECONNREFUSED counts as a failure of the jail: the call was allowed.
"""

import errno
import socket

_BLOCKED = {errno.EPERM, errno.EACCES, 10013}


class Strategy:
    def on_bar(self, bar, state):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        except OSError as exc:
            return 0.0 if exc.errno in _BLOCKED else 1.0
        try:
            sock.connect(("127.0.0.1", 1))
        except OSError as exc:
            return 0.0 if exc.errno in _BLOCKED else 1.0
        finally:
            sock.close()
        return 1.0
