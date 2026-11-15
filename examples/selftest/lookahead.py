
import os


class Strategy:
    def on_bar(self, bar, state):
        os.read(0, 1)
        return 0.0
