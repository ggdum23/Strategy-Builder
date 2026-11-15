

class Strategy:
    def on_bar(self, bar, state):
        raise RuntimeError("referee selftest crash")
