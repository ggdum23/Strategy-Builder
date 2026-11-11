"""Long/flat trend strategy for ASSET.

Decision: once per regular session, on the last RTH bar (minute 959, or 779 on a
shortened session). The order fills at the next session's open. No extended hours.

Rule (all inputs are regular-session closes already known at that bar):
- Let C be the close history, including today.
- Until 253 closes exist, stay flat.
- A horizon h in {63, 126, 252} is ON when C[-1] > C[-1-h].
- If fewer than 2 horizons are ON, desired exposure is 0.
- Otherwise annualized volatility is the sample standard deviation (ddof=1) of the
  last 60 close-to-close returns, times sqrt(252).
  Raw exposure = min(1.0, 0.15 / volatility), rounded half-up to a 0.25 step.
- The target changes only when the new desired exposure differs from the current
  target by at least 0.25. Otherwise the previous target is kept.

Long or flat only. Gross exposure is in {0, 0.25, 0.5, 0.75, 1.0}.
"""
import math


class Strategy:
    WANTS_EXTENDED_HOURS = False

    HORIZONS = (63, 126, 252)
    VOL_LOOKBACK = 60
    VOL_TARGET = 0.15
    MAX_LEV = 1.0
    STEP = 0.25
    BAND = 0.25
    MIN_CLOSES = 252 + 1  # need C[t] and C[t-252]

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
            # Session rolled without a terminal bar: keep one close per session.
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
