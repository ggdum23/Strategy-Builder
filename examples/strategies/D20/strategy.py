"""Contrarian pullback with a trend filter and a volatility kill-switch.

Decision once per regular session, on the last RTH bar (15:59, or 12:59 on a
shortened session). The order fills at the next delivered bar's open, which is
the next session's open. Extended hours are not used.

Until 200 session closes have been seen the target is +1.0 (there is nothing
to estimate yet). After that the target is a pure function of closes already
in hand:

- trend up:        last close > 200-session simple mean
- pullback:        last close is 5% or more below the 20-session high
- vol kill:        stdev of the last 20 close-to-close returns, annualized
                   with sqrt(252), is at least 30%

Priority (first match wins):
1. Vol kill and trend broken  -> 0.0  (flat)
2. Vol kill or trend broken   -> 0.5
3. Trend up and pullback      -> 2.0
4. Otherwise                  -> 1.0

No shorts. Targets are discrete, so an unchanged state sends no order.
"""

import math


class Strategy:
    WANTS_EXTENDED_HOURS = False

    SMA_LOOKBACK = 200
    HIGH_LOOKBACK = 20
    VOL_LOOKBACK = 20
    VOL_KILL = 0.30
    DIP = -0.05
    TARGET_DIP = 2.0
    TARGET_BASE = 1.0
    TARGET_DEFENSIVE = 0.5
    TARGET_FLAT = 0.0

    def __init__(self):
        self.closes = []
        self.target = 0.0

    def on_bar(self, bar, state):
        minute = bar["minute"]
        last_minute = 779 if bar["short_session"] else 959
        if minute != last_minute:
            return None
        close = float(bar["close"])
        if not math.isfinite(close) or close <= 0.0:
            return None
        self.closes.append(close)
        if len(self.closes) > self.SMA_LOOKBACK:
            del self.closes[0]
        desired = self._desired()
        if desired == self.target:
            return None
        self.target = desired
        return desired

    def _desired(self):
        n = len(self.closes)
        if n < self.SMA_LOOKBACK:
            return self.TARGET_BASE
        sma = sum(self.closes) / n
        hi = max(self.closes[-self.HIGH_LOOKBACK :])
        # 20 close-to-close returns end on today's close (21 prices).
        rets = []
        start = n - (self.VOL_LOOKBACK + 1)
        for i in range(start + 1, n):
            prev = self.closes[i - 1]
            rets.append(self.closes[i] / prev - 1.0)
        vol = _sample_std(rets) * math.sqrt(252.0)
        last = self.closes[-1]
        up = last > sma
        dip = last / hi - 1.0 <= self.DIP
        kill = vol >= self.VOL_KILL
        if kill and not up:
            return self.TARGET_FLAT
        if kill or not up:
            return self.TARGET_DEFENSIVE
        if dip:
            return self.TARGET_DIP
        return self.TARGET_BASE


def _sample_std(values):
    m = sum(values) / len(values)
    var = sum((x - m) * (x - m) for x in values) / (len(values) - 1)
    return math.sqrt(var) if var > 0.0 else 0.0
