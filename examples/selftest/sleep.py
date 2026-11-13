"""Sleep past the per-call limit. The referee disqualifies the run."""

import time


class Strategy:
    def on_bar(self, bar, state):
        time.sleep(30)
        return 0.0
