
import math


class Strategy:
    WANTS_EXTENDED_HOURS = False

    LOOKBACK = 20
    TARGET_VOL = 0.18
    STEP = 0.25
    CAP = 1.0
    GRID = (0.0, 0.25, 0.5, 0.75, 1.0)

    def __init__(self):
        self.prev_minute = None
        self.today_close = None
        self.closes = []

    def on_bar(self, bar, state):
        try:
            minute = int(bar["minute"])
            close = float(bar["close"])
        except (KeyError, TypeError, ValueError):
            return None
        if not math.isfinite(close) or close <= 0.0:
            return None

        if self.prev_minute is None or minute < self.prev_minute:
            if self.today_close is not None and self.today_close > 0.0:
                self.closes.append(self.today_close)
                if len(self.closes) > self.LOOKBACK:
                    del self.closes[: len(self.closes) - self.LOOKBACK]
            self.today_close = close
        else:
            self.today_close = close
        self.prev_minute = minute

        try:
            short = int(bar["short_session"]) == 1
        except (KeyError, TypeError, ValueError):
            short = False
        decision_minute = 770 if short else 950
        if minute != decision_minute:
            return None
        return self._target(close)

    def _target(self, close):
        if len(self.closes) < self.LOOKBACK:
            return 0.0
        window = self.closes[-self.LOOKBACK :] + [close]
        rets = []
        for i in range(1, len(window)):
            prev = window[i - 1]
            if prev <= 0.0:
                return 0.0
            rets.append(window[i] / prev - 1.0)
        n = len(rets)
        if n < 2:
            return 0.0
        mu = sum(rets) / n
        var = sum((r - mu) ** 2 for r in rets) / (n - 1)
        if not math.isfinite(var) or var <= 0.0:
            return 0.0
        rv = math.sqrt(var) * math.sqrt(252.0)
        if not math.isfinite(rv) or rv < 1e-8:
            return 0.0
        raw = self.TARGET_VOL / rv
        if raw > self.CAP:
            raw = self.CAP
        if raw < 0.0:
            raw = 0.0
        k = int(math.floor(raw / self.STEP + 0.5))
        if k < 0:
            k = 0
        if k > 4:
            k = 4
        return self.GRID[k]
