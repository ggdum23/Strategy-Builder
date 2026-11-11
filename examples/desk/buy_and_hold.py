"""Desk 1x buy and hold. One target, then the engine holds it. No drift rebalance."""


class Strategy:
    def on_bar(self, bar, account):
        return 1.0
