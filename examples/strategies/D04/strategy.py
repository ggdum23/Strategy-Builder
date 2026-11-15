import math


class Strategy:
    WANTS_EXTENDED_HOURS = False

    MA_SESSIONS = 180
    VOL_SESSIONS = 20
    VOL_LIMIT = 0.20
    TARGET_LONG = 1.0
    TARGET_FLAT = 0.0
    DECISION_MINUTE = 958
    DECISION_MINUTE_SHORT = 778

    def __init__(self):
        self.prices = []
        self._last_decision_day = None

    def on_bar(self, bar, state):
        try:
            minute = int(bar["minute"])
            day = int(bar["day"])
            short = int(bar["short_session"])
            px = float(bar["close"])
        except (KeyError, TypeError, ValueError):
            return None
        if not math.isfinite(px) or px <= 0.0:
            return None
        decision = self.DECISION_MINUTE_SHORT if short else self.DECISION_MINUTE
        if minute != decision:
            return None
        if self._last_decision_day == day:
            return None
        self._last_decision_day = day
        self.prices.append(px)
        keep = self.MA_SESSIONS + 1
        if len(self.prices) > keep:
            del self.prices[: len(self.prices) - keep]
        return self._target()

    def _target(self):
        p = self.prices
        n = self.MA_SESSIONS
        w = self.VOL_SESSIONS
        if len(p) < n:
            return self.TARGET_LONG
        ma = sum(p[-n:]) / n
        rets = []
        start = len(p) - w
        for i in range(start, len(p)):
            prev = p[i - 1]
            if prev <= 0.0:
                return self.TARGET_LONG
            rets.append(p[i] / prev - 1.0)
        mean = sum(rets) / w
        var = sum((r - mean) ** 2 for r in rets) / (w - 1)
        rv = math.sqrt(var * 252.0) if var > 0.0 else 0.0
        if p[-1] < ma and rv > self.VOL_LIMIT:
            return self.TARGET_FLAT
        return self.TARGET_LONG

    def on_day_end(self, summary):
        return None
