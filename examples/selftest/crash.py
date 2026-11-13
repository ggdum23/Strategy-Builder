"""Raise on the first bar. The referee disqualifies the run."""


class Strategy:
    def on_bar(self, bar, state):
        raise RuntimeError("referee selftest crash")
