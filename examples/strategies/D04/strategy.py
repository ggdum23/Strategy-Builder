"""Overnight-premium hold with a trend-and-volatility risk-off.

ASSET's drift is earned mostly by holding across the session close. A round trip
that owns only the overnight gap costs more than that gap is worth, so this
strategy does not day-trade. It stays long (target +1) and steps aside only when
both of the following are true at the pre-close decision bar:

- the decision price is below its 180-session moving average, and
- 20-session realized volatility, annualized, is above 20%.

Otherwise the target is +1. No shorts, no leverage, one decision per regular
session. Extended hours are not traded; a long target is held through the close
and the overnight gap, and a flat target is flat for both.
"""
import math


class Strategy:
    WANTS_EXTENDED_HOURS = False

    MA_SESSIONS = 180
    VOL_SESSIONS = 20
    VOL_LIMIT = 0.20  # annualized, sample std * sqrt(252)
    TARGET_LONG = 1.0
    TARGET_FLAT = 0.0
    # Bar-start minute of the decision. Fill is the next bar's open, still
    # before the regular-session close (15:59 full session, 12:59 short).
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
        # 180 prices for the average; 21 prices for 20 close-to-close returns.
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
        # Sample standard deviation of the last w close-to-close decision returns.
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
