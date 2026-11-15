import math


class Strategy:
    WANTS_EXTENDED_HOURS = False

    HORIZONS = (63, 126, 252)
    VOL_LOOKBACK = 60
    VOL_TARGET = 0.15
    MAX_LEV = 1.0
    STEP = 0.25
    BAND = 0.25
    MIN_CLOSES = 252 + 1

    def __init__(self):
        self.closes = []
        self.cur_day = None
        self.last_close = None
        self.pushed_today = False

    def on_bar(self, bar, state):
        day = int(bar["day"])
        minute = int(bar["minute"])
        close = float(bar["close"])
        short = int(bar["short_session"])
        if not math.isfinite(close) or close <= 0.0:
            return None

        if self.cur_day is None:
            self.cur_day = day
        elif day != self.cur_day:
            if not self.pushed_today and self.last_close is not None:
                self._push(self.last_close)
            self.pushed_today = False
            self.cur_day = day

        self.last_close = close
        end_minute = 779 if short else 959
        if minute != end_minute:
            return None
        if not self.pushed_today:
            self._push(close)
            self.pushed_today = True
        return self._target(state)

    def _push(self, close):
        self.closes.append(close)
        if len(self.closes) > 320:
            del self.closes[: len(self.closes) - 320]

    def _target(self, state):
        c = self.closes
        if len(c) < self.MIN_CLOSES or len(c) < self.VOL_LOOKBACK + 1:
            return None
        votes = 0
        for h in self.HORIZONS:
            if c[-1] > c[-1 - h]:
                votes += 1
        if votes < 2:
            desired = 0.0
        else:
            n = self.VOL_LOOKBACK
            rets = [c[-1 - k] / c[-2 - k] - 1.0 for k in range(n)]
            mean = sum(rets) / n
            var = sum((x - mean) ** 2 for x in rets) / (n - 1)
            if var <= 0.0:
                raw = self.MAX_LEV
            else:
                rv = math.sqrt(var * 252.0)
                raw = self.MAX_LEV if rv < 1e-8 else min(self.MAX_LEV, self.VOL_TARGET / rv)
            steps = math.floor(raw / self.STEP + 0.5)
            desired = steps * self.STEP
            if desired < 0.0:
                desired = 0.0
            elif desired > self.MAX_LEV:
                desired = self.MAX_LEV
        cur = float(state["target"])
        if abs(desired - cur) + 1e-12 < self.BAND:
            return None
        return desired
