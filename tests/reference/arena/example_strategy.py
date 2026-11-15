

class Strategy:
    WANTS_EXTENDED_HOURS = False

    def __init__(self):
        self.alpha = 2.0 / (3900 + 1)
        self.ema = None

    def on_bar(self, bar, state):
        c = bar["close"]
        self.ema = c if self.ema is None else self.ema + self.alpha * (c - self.ema)
        decision_minute = 770 if bar["short_session"] else 950
        if bar["minute"] != decision_minute:
            return None
        return 1.0 if c > self.ema else 0.0

    def on_day_end(self, summary):
        pass
