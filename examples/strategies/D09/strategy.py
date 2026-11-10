"""Defensive volatility scaler for ASSET.

Long by default, half size when recent volatility is elevated, flat only in
extreme volatility. Never short, never levered above 1x. One decision per
session, on the bar that starts one minute before the regular-session close,
so the fill (next bar open) is held through the close and the overnight.

Uses only bars already received. Standard library + numpy.
"""
import math

import numpy as np

# --- hard-coded, chosen on the design window (days 0..2999) ---
_FAST = 10                 # sessions in the fast realized-vol window
_SLOW = 40                 # sessions in the slow realized-vol window
_TARGET_VOL = 0.15         # annualized volatility the sizer aims at
_STEP = 0.50               # position bucket
_MAX_LEV = 1.0             # gross cap; shorts are not used
_ANN = math.sqrt(252.0)
_DECISION_MINUTE = 958     # 15:58, one minute before the 16:00 close
_DECISION_MINUTE_SHORT = 778  # 12:58, one minute before a 13:00 close


# raw = min(MAX_LEV, TARGET_VOL / sigma), then round half-up onto a 0.5 grid.
# The decision boundaries of that rounding, solved for sigma, are:
#   sigma <= TARGET_VOL / 0.75  -> 1.0     (0.15/0.75 = 0.20)
#   sigma <= TARGET_VOL / 0.25  -> 0.5     (0.15/0.25 = 0.60)
#   otherwise                   -> 0.0
# Comparisons, not a division at the boundary, so 0.15/0.20 float error cannot
# flip a 20% vol day from full size to half size.
_FULL_AT = _TARGET_VOL / (1.5 * _STEP)   # 0.20
_HALF_AT = _TARGET_VOL / (0.5 * _STEP)   # 0.60


def _bucket(sigma):
    """Map annualized sigma to a target in {0.0, 0.5, 1.0}."""
    if not math.isfinite(sigma) or sigma <= _FULL_AT:
        return _MAX_LEV
    if sigma <= _HALF_AT:
        return _STEP
    return 0.0


class Strategy:
    WANTS_EXTENDED_HOURS = False

    def __init__(self):
        # Decision prices only (one per session). 41 prices = 40 returns.
        self._px = []
        self._target = 0.0

    def on_bar(self, bar, state):
        minute = bar["minute"]
        if minute != _DECISION_MINUTE and minute != _DECISION_MINUTE_SHORT:
            return None
        short = bar["short_session"]
        want = _DECISION_MINUTE_SHORT if short else _DECISION_MINUTE
        if minute != want:
            return None
        try:
            px = float(bar["close"])
        except (TypeError, ValueError):
            return None
        if not math.isfinite(px) or px <= 0.0:
            return None
        buf = self._px
        buf.append(px)
        # Keep the slow window plus one extra price so the return count is exact.
        extra = len(buf) - (_SLOW + 1)
        if extra > 0:
            del buf[:extra]
        w = self._weight()
        if w == self._target:
            return None
        self._target = w
        return w

    def _weight(self):
        buf = self._px
        n = len(buf)
        if n < _SLOW + 1:
            return 0.0
        arr = np.asarray(buf, dtype=np.float64)
        prev = arr[:-1]
        rets = arr[1:] / prev - 1.0
        # Both windows end on this session's return. Sample stdev (ddof=1).
        s_fast = float(rets[-_FAST:].std(ddof=1))
        s_slow = float(rets.std(ddof=1))
        sigma = s_fast if s_fast > s_slow else s_slow
        return _bucket(sigma * _ANN)
