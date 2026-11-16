

class Strategy:
    WANTS_EXTENDED_HOURS = False

    LOOKBACKS = (60, 120, 252)
    MIN_VOTES = 2
    EXPOSURE = 1.0
    DECISION_OFFSET = 31
    MAX_CLOSES = 320

    def __init__(self):
        self.closes = []

    def on_bar(self, bar, state):
        end = 780 if bar["short_session"] else 960
        if bar["minute"] != end - self.DECISION_OFFSET:
            return None
        px = float(bar["close"])
        if not px > 0.0:
            return 0.0
        npos = 0
        avail = 0
        n = len(self.closes)
        for L in self.LOOKBACKS:
            if n >= L:
                avail += 1
                if px > self.closes[-L]:
                    npos += 1
        if avail < self.MIN_VOTES:
            return 0.0
        return self.EXPOSURE if npos >= self.MIN_VOTES else 0.0

    def on_day_end(self, summary):
        c = summary.get("rth_close")
        if c is None:
            return
        c = float(c)
        if not c > 0.0:
            return
        self.closes.append(c)
        if len(self.closes) > self.MAX_CLOSES:
            del self.closes[: len(self.closes) - self.MAX_CLOSES]
