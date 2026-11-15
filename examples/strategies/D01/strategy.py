import math


class Strategy:
    WANTS_EXTENDED_HOURS = False

    TARGET_VOL = 0.22
    VOL_FLOOR = 0.05
    CAP = 1.5
    STEP = 0.25
    FAST = 21
    SLOW = 63
    LAST_MINUTE = 15 * 60 + 59
    LAST_MINUTE_SHORT = 12 * 60 + 59

    def __init__(self):
        self.closes = []
        self.cur_day = None
        self.cur_close = None
        self.finalized = False

    def on_bar(self, bar, state):
        try:
            return self._on_bar(bar, state)
        except Exception:
            return None

    def _on_bar(self, bar, state):
        day = int(bar["day"])
        close = float(bar["close"])
        minute = int(bar["minute"])
        if not (close > 0.0):
            return None
        short = int(bar["short_session"])
        if self.cur_day is None:
            self.cur_day = day
            self.cur_close = close
            self.finalized = False
            return None
        if day != self.cur_day:
            target = None
            if not self.finalized and self.cur_close is not None and self.cur_close > 0.0:
                self._append(self.cur_close)
                self.finalized = True
                target = self._target()
            self.cur_day = day
            self.cur_close = close
            self.finalized = False
            return target
        self.cur_close = close
        decision = self.LAST_MINUTE_SHORT if short else self.LAST_MINUTE
        if minute == decision and not self.finalized:
            self._append(close)
            self.finalized = True
            return self._target()
        return None

    def _append(self, close):
        self.closes.append(close)
        if len(self.closes) > 80:
            del self.closes[:-80]

    def _sample_std(self, n_returns):
        need = n_returns + 1
        w = self.closes[-need:]
        if len(w) < need:
            return None
        n = n_returns
        s = 0.0
        s2 = 0.0
        prev = w[0]
        for i in range(1, need):
            px = w[i]
            r = px / prev - 1.0
            prev = px
            s += r
            s2 += r * r
        var = (s2 - (s * s) / n) / (n - 1)
        if var < 0.0:
            var = 0.0
        return math.sqrt(var)

    def _target(self):
        if len(self.closes) < self.SLOW + 1:
            return 0.0
        fast = self._sample_std(self.FAST)
        slow = self._sample_std(self.SLOW)
        if fast is None or slow is None:
            return 0.0
        rv = 0.5 * (fast + slow) * math.sqrt(252.0)
        if rv < self.VOL_FLOOR:
            rv = self.VOL_FLOOR
        raw = self.TARGET_VOL / rv
        if raw > self.CAP:
            raw = self.CAP
        if raw < 0.0:
            raw = 0.0
        ticks = int(math.floor(raw / self.STEP + 0.5))
        max_ticks = int(round(self.CAP / self.STEP))
        if ticks < 0:
            ticks = 0
        elif ticks > max_ticks:
            ticks = max_ticks
        return ticks * self.STEP
