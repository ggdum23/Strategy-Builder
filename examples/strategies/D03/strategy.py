"""Vol-budget strategy for ASSET.

Once per regular session, at the close, set a long-only exposure in {0, 0.25, 0.50, 0.75, 1.0}
equal to 0.18 / blended realised volatility, clipped to [0, 1] and stepped to the nearest
quarter. Blended vol is the average of 20-session and 60-session close-to-close realised
volatility. The position fills at the next session's open. No shorts, no extended-hours
trading, no leverage above 1x.

Runtime dependency: the Python standard library only. All parameters are constants.
"""
import math


class Strategy:
    WANTS_EXTENDED_HOURS = False

    # Annualised volatility budget. Exposure = clip(TARGET_VOL / RV, 0, MAX_EXPOSURE).
    TARGET_VOL = 0.18
    MAX_EXPOSURE = 1.0
    MIN_EXPOSURE = 0.0
    FAST = 20
    SLOW = 60
    STEP = 0.25
    # Rebalance only when the stepped weight moves by at least one notch.
    BAND = 0.25
    # Held until both vol windows are full (known at that session's close).
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
        # One decision per session, on the last regular-session bar (15:59, or
        # 12:59 when the session is shortened). A bar at or after that minute
        # is late enough; the delivered grid's last bar is exactly that minute.
        decision_minute = 779 if int(bar["short_session"]) else 959
        if bar["minute"] < decision_minute or day == self._decided_day:
            return None
        self._decided_day = day

        close = float(bar["close"])
        if not math.isfinite(close) or close <= 0.0:
            return None
        if self.prev_close is not None and self.prev_close > 0.0:
            self.rets.append(close / self.prev_close - 1.0)
            # Bound memory on a long forward run; the slow window is 60.
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
                # Half-up step onto the quarter grid. Quarters are binary-exact.
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
        # Two-pass sample standard deviation, annualised with 252 sessions.
        mean = 0.0
        for x in window:
            mean += x
        mean /= n
        acc = 0.0
        for x in window:
            d = x - mean
            acc += d * d
        return math.sqrt(acc / (n - 1) * 252.0)
