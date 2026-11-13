"""Return 0.0 when numpy imports and a thread can run a dot product."""

import threading

import numpy as np


class Strategy:
    def on_bar(self, bar, state):
        started = []

        def work():
            started.append(int(np.dot(np.arange(4), np.arange(4))))

        thread = threading.Thread(target=work)
        thread.start()
        thread.join(timeout=5)
        if thread.is_alive() or started != [14]:
            return 1.0
        return 0.0
