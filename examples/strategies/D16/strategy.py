"""Trend stand-aside: long ASSET, flat only when both horizons are down.

Decision once per regular session, on the last RTH bar (minute 959, or 779 on a
short session). The order fills at the next session's open. No extended-hours
trading, no shorts, no leverage.

Rule, using only session closes already received:
    slow = close[t] / close[t-120] - 1
    fast = close[t] / close[t-20] - 1
    target = 0  if slow < 0 and fast < 0 else 1
Until 121 session closes have been seen, the target is flat.
"""

from collections import deque


class Strategy:
    WANTS_EXTENDED_HOURS = False

    SLOW = 120
    FAST = 20
    LONG = 1.0
    FLAT = 0.0
    # Last regular-session bar start: 15:59, or 12:59 on a shortened session.
    LAST_MINUTE = 959
    LAST_MINUTE_SHORT = 779

    def __init__(self):
        self.closes = deque(maxlen=self.SLOW + 1)
        self.day = None
        self.committed = False

    def on_bar(self, bar, state):
        try:
            day = int(bar["day"])
            minute = int(bar["minute"])
            short = int(bar["short_session"])
            close = float(bar["close"])
        except (KeyError, TypeError, ValueError):
            return None
        if close != close or close <= 0.0:  # NaN or non-positive
            return None

        if self.day is None or day != self.day:
            self.day = day
            self.committed = False

        last = self.LAST_MINUTE_SHORT if short else self.LAST_MINUTE
        if minute != last or self.committed:
            return None

        self.closes.append(close)
        self.committed = True
        if len(self.closes) < self.SLOW + 1:
            return self.FLAT

        slow = close / self.closes[0] - 1.0
        fast = close / self.closes[-(self.FAST + 1)] - 1.0
        if slow < 0.0 and fast < 0.0:
            return self.FLAT
        return self.LONG
