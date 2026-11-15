import math


class Strategy:
    WANTS_EXTENDED_HOURS = False

    TARGET_VOL = 0.18
    MAX_EXPOSURE = 1.0
    MIN_EXPOSURE = 0.0
    FAST = 20
    SLOW = 60
    STEP = 0.25
    BAND = 0.25
    WARM_EXPOSURE = 1.0

    def __init__(self):
        self.rets = []
        self.prev_close = None
        self.last_target = 0.0
        self._decided_day = None

    def on_bar(self, bar, state):
        try:
            return self._on_bar(bar, state)
        except Exception:
            return None

    def _on_bar(self, bar, state):
        day = bar["day"]
        decision_minute = 779 if int(bar["short_session"]) else 959
        if bar["minute"] < decision_minute or day == self._decided_day:
            return None
        self._decided_day = day

        close = float(bar["close"])
        if not math.isfinite(close) or close <= 0.0:
            return None
        if self.prev_close is not None and self.prev_close > 0.0:
            self.rets.append(close / self.prev_close - 1.0)
            if len(self.rets) > self.SLOW + 5:
                del self.rets[: len(self.rets) - self.SLOW]
        self.prev_close = close

        if len(self.rets) < self.SLOW:
            target = self.WARM_EXPOSURE
        else:
            fast = self._ann_vol(self.FAST)
            slow = self._ann_vol(self.SLOW)
            blended = 0.5 * fast + 0.5 * slow
            if not math.isfinite(blended) or blended <= 1e-8:
                target = self.WARM_EXPOSURE
            else:
                raw = self.TARGET_VOL / blended
                if raw < self.MIN_EXPOSURE:
                    raw = self.MIN_EXPOSURE
                elif raw > self.MAX_EXPOSURE:
                    raw = self.MAX_EXPOSURE
                target = math.floor(raw / self.STEP + 0.5) * self.STEP
                if target < self.MIN_EXPOSURE:
                    target = self.MIN_EXPOSURE
                elif target > self.MAX_EXPOSURE:
                    target = self.MAX_EXPOSURE
        if abs(target - self.last_target) + 1e-12 < self.BAND:
            return None
        self.last_target = target
        return target

    def _ann_vol(self, n):
        window = self.rets[-n:]
        mean = 0.0
        for x in window:
            mean += x
        mean /= n
        acc = 0.0
        for x in window:
            d = x - mean
            acc += d * d
        return math.sqrt(acc / (n - 1) * 252.0)
