"""Raise KeyboardInterrupt. The child must report it, not die on the signal."""


class Strategy:
    def on_bar(self, bar, state):
        raise KeyboardInterrupt
