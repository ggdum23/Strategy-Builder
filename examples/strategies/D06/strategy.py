"""Volatility-budget long for ASSET.

Once per regular session, at the close, set a target exposure in [0, 1]:

    rv_fast = std(last 21 session returns) * sqrt(252)
    rv_slow = std(last 63 session returns) * sqrt(252)
    rv      = 0.5 * rv_fast + 0.5 * rv_slow
    raw     = clip(0.18 / rv, 0, 1)

The target is sent only when it differs from the live target by at least 0.10
(after rounding to the nearest 0.01). The engine fills the new target at the
next session's open. No shorts, no leverage above 1, no extended-hours orders.

All inputs are session closes already printed. The first 63 completed
session-to-session returns are a warm-up; until then the strategy stays flat.
"""

import math

import numpy as np


class Strategy:
    WANTS_EXTENDED_HOURS = False

    # Hard-coded risk model. Chosen on the design window; see REPORT.md.
    FAST = 21
    SLOW = 63
    TARGET_VOL = 0.18
    MAX_EXPOSURE = 1.0
    BAND = 0.10
    SESSIONS_PER_YEAR = 252
    # Bar-start minute of the last regular-session bar (16:00 close, or 13:00
    # when short_session is set). Both are properties of the delivered grid.
    LAST_MINUTE = 959
    LAST_MINUTE_SHORT = 779

    def __init__(self):
        self.rets = []
        self.prev_close = None
        self.last = 0.0
        self.emitted = False
        self._ann = math.sqrt(self.SESSIONS_PER_YEAR)

    def on_bar(self, bar, state):
        minute = int(bar["minute"])
        last = self.LAST_MINUTE_SHORT if int(bar["short_session"]) else self.LAST_MINUTE
        if minute != last:
            return None

        close = float(bar["close"])
        if not math.isfinite(close) or close <= 0.0:
            return None

        prev = self.prev_close
        self.prev_close = close
        if prev is None or not math.isfinite(prev) or prev <= 0.0:
            return None

        ret = close / prev - 1.0
        if not math.isfinite(ret):
            return None
        self.rets.append(ret)

        n = len(self.rets)
        if n < self.SLOW:
            return None

        fast = float(np.std(self.rets[-self.FAST :], ddof=1)) * self._ann
        slow = float(np.std(self.rets[-self.SLOW :], ddof=1)) * self._ann
        rv = 0.5 * fast + 0.5 * slow
        if not math.isfinite(rv) or rv <= 1e-8:
            return None

        raw = self.TARGET_VOL / rv
        if raw < 0.0:
            raw = 0.0
        elif raw > self.MAX_EXPOSURE:
            raw = self.MAX_EXPOSURE

        if self.emitted and abs(raw - self.last) < self.BAND:
            return None

        snapped = round(raw, 2)
        if snapped < 0.0:
            snapped = 0.0
        elif snapped > self.MAX_EXPOSURE:
            snapped = self.MAX_EXPOSURE
        if snapped == self.last:
            self.emitted = True
            return None

        self.last = snapped
        self.emitted = True
        return snapped
