"""Volatility-managed long position.

Once per regular session, on the last bar, set exposure to
round(min(1.5, 0.15 / rv) / 0.5) * 0.5, where rv is the annualized standard
deviation of the last 20 session returns. The order fills at the next
session's open. The position is always long or, in extreme volatility, flat.
No shorts. Regular session only.

All parameters are fixed constants. State is built only from bars passed to on_bar.
"""
from collections import deque

import numpy as np


class Strategy:
    WANTS_EXTENDED_HOURS = False

    VOL_N = 20
    VOL_TARGET = 0.15
    MAX_LEV = 1.5
    STEP = 0.5

    def __init__(self):
        self.rets = deque(maxlen=self.VOL_N)
        self.prev_close = None
        self.target = 0.0

    def on_bar(self, bar, state):
        end = 779 if bar["short_session"] else 959
        if bar["minute"] != end:
            return None
        close = bar["close"]
        if close is None or close != close or close <= 0.0:
            return None
        prev = self.prev_close
        self.prev_close = close
        if prev is None or prev <= 0.0:
            return None
        self.rets.append(close / prev - 1.0)
        if len(self.rets) < self.VOL_N:
            return None
        rv = float(np.std(self.rets, ddof=1) * np.sqrt(252.0))
        if not np.isfinite(rv) or rv <= 1e-8:
            return None
        raw = min(self.MAX_LEV, self.VOL_TARGET / rv)
        raw = float(np.round(raw / self.STEP) * self.STEP)
        if raw < 0.0:
            raw = 0.0
        elif raw > self.MAX_LEV:
            raw = self.MAX_LEV
        if abs(raw - self.target) < 1e-12:
            return None
        self.target = raw
        return raw
