import math


class Strategy:
    WANTS_EXTENDED_HOURS = False

    PEAK_WINDOW = 100
    EXIT_DD = 0.20
    BREAKOUT = 20
    LAST_MINUTE = 959
    LAST_MINUTE_SHORT = 779

    def __init__(self):
        self.closes = []
        self.long = True
        self.last_target = None
        self.faults = 0

    def on_bar(self, bar, state):
        try:
            return self._decide(bar)
        except Exception:
            self.faults += 1
            return None

    def _decide(self, bar):
        minute = int(bar["minute"])
        last = self.LAST_MINUTE_SHORT if int(bar["short_session"]) else self.LAST_MINUTE
        if minute != last:
            return None
        close = float(bar["close"])
        if not math.isfinite(close) or close <= 0.0:
            return None
        self.closes.append(close)
        if len(self.closes) > self.PEAK_WINDOW:
            del self.closes[0]
        target = 1.0 if self._want_long(close) else 0.0
        if self.last_target is not None and target == self.last_target:
            return None
        self.last_target = target
        return target

    def _want_long(self, close):
        if len(self.closes) < self.PEAK_WINDOW:
            self.long = True
            return True
        peak = max(self.closes)
        dd = close / peak - 1.0
        if self.long:
            if dd <= -self.EXIT_DD:
                self.long = False
        else:
            prior = max(self.closes[-(self.BREAKOUT + 1):-1])
            if close > prior and dd > -self.EXIT_DD:
                self.long = True
        return self.long
