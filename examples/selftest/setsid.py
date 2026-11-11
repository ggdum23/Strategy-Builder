"""Return 0.0 when setsid is refused, else 1.0.

Windows has no setsid. The job object's active-process limit is the spawn
check in fork.py.
"""

import os


class Strategy:
    def on_bar(self, bar, state):
        setsid = getattr(os, "setsid", None)
        if setsid is None:
            return 0.0
        try:
            setsid()
        except OSError:
            return 0.0
        return 1.0
