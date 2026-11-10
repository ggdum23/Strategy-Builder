"""Trailing-drawdown participation for ASSET.

Fully automated. One decision per regular session, on the last bar
(minute 959, or 779 when short_session is set). The order fills at the
next regular-session open. Extended hours are not used.

Rules, in priority order, using only closes already received:

1. Until 100 session closes are in hand, stay long (target +1). There is
   no peak yet, and the account is here to participate.
2. If long and the close is 20% or more below the highest close of the
   last 100 sessions (today included), go flat (target 0).
3. If flat, go long again only when BOTH are true: the close is back
   inside that 20% band, and the close is strictly above the highest
   close of the prior 20 sessions (today excluded).
4. Otherwise keep the current target.

No shorts, no leverage, no intraday stop. Parameters are frozen constants.
"""
import math


class Strategy:
    WANTS_EXTENDED_HOURS = False

    PEAK_WINDOW = 100
    EXIT_DD = 0.20
    BREAKOUT = 20
    LAST_MINUTE = 959          # last regular bar start; session ends 16:00
    LAST_MINUTE_SHORT = 779    # shortened session ends 13:00

    def __init__(self):
        self.closes = []
        self.long = True
        self.last_target = None
        self.faults = 0

    def on_bar(self, bar, state):
        try:
            return self._decide(bar)
        except Exception:
            self.faults += 1
            return None

    def _decide(self, bar):
        minute = int(bar["minute"])
        last = self.LAST_MINUTE_SHORT if int(bar["short_session"]) else self.LAST_MINUTE
        if minute != last:
            return None
        close = float(bar["close"])
        if not math.isfinite(close) or close <= 0.0:
            return None
        self.closes.append(close)
        if len(self.closes) > self.PEAK_WINDOW:
            del self.closes[0]
        target = 1.0 if self._want_long(close) else 0.0
        if self.last_target is not None and target == self.last_target:
            return None
        self.last_target = target
        return target

    def _want_long(self, close):
        if len(self.closes) < self.PEAK_WINDOW:
            self.long = True
            return True
        peak = max(self.closes)
        dd = close / peak - 1.0
        if self.long:
            if dd <= -self.EXIT_DD:
                self.long = False
        else:
            prior = max(self.closes[-(self.BREAKOUT + 1):-1])
            if close > prior and dd > -self.EXIT_DD:
                self.long = True
        return self.long
