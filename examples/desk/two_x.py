"""Desk constant 2x long. Financing is the desk rate (7% unless the run overrides it)."""


class Strategy:
    def on_bar(self, bar, account):
        return 2.0
