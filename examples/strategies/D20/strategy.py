
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
