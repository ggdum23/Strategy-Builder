import math


def _quantize(x, step=0.05):
    if x == 0.0 or not math.isfinite(x):
        return 0.0
    ax = math.floor(abs(x) / step + 0.5) * step
    return math.copysign(ax, x)


class Strategy:
    WANTS_EXTENDED_HOURS = False

    RV_LOOKBACK = 10
    TARGET_VOL = 0.10
    MAX_LEV = 1.0
    RV_FLOOR = 0.06
    BAND = 0.10
    STEP = 0.05

    def __init__(self):
        self.closes = []
        self.committed = 0.0
        self._sqrt252 = math.sqrt(252.0)

    def on_bar(self, bar, state):
        last = 779 if bar["short_session"] else 959
        if bar["minute"] != last:
            return None
        px = bar["close"]
        if not math.isfinite(px) or px <= 0.0:
            return None
        self.closes.append(px)
        need = self.RV_LOOKBACK + 1
        if len(self.closes) > need:
            del self.closes[0]
        if len(self.closes) < need:
            self.committed = 0.0
            return 0.0
        n = self.RV_LOOKBACK
        cs = self.closes
        rets = [cs[i] / cs[i - 1] - 1.0 for i in range(1, n + 1)]
        mean = sum(rets) / n
        var = sum((ri - mean) ** 2 for ri in rets) / (n - 1)
        if var < 0.0:
            var = 0.0
        rv = math.sqrt(var) * self._sqrt252
        if rv < self.RV_FLOOR:
            rv = self.RV_FLOOR
        raw = self.TARGET_VOL / rv
        if raw > self.MAX_LEV:
            raw = self.MAX_LEV
        if raw < 0.0:
            raw = 0.0
        tgt = _quantize(raw, self.STEP)
        if abs(tgt - self.committed) < self.BAND - 1e-15:
            tgt = self.committed
        self.committed = tgt
        return tgt

    def on_day_end(self, summary):
        return None
