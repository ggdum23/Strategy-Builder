"""Return 0.0 when a new process cannot be started, else 1.0."""

import os
import sys


class Strategy:
    def on_bar(self, bar, state):
        if os.name == "nt":
            import subprocess

            try:
                proc = subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError:
                return 0.0
            proc.kill()
            return 1.0
        try:
            pid = os.fork()
        except OSError:
            return 0.0
        if pid == 0:
            os._exit(0)
        return 1.0
