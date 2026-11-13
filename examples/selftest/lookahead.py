"""Read stdin, which is the next bar that has not been sent yet.

The read blocks until the per-call timeout. The referee disqualifies it.
"""

import os


class Strategy:
    def on_bar(self, bar, state):
        os.read(0, 1)
        return 0.0
