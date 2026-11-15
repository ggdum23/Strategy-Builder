import math

import numpy as np

_FAST = 10
_SLOW = 40
_TARGET_VOL = 0.15
_STEP = 0.50
_MAX_LEV = 1.0
_ANN = math.sqrt(252.0)
_DECISION_MINUTE = 958
_DECISION_MINUTE_SHORT = 778


_FULL_AT = _TARGET_VOL / (1.5 * _STEP)
_HALF_AT = _TARGET_VOL / (0.5 * _STEP)


def _bucket(sigma):
    if not math.isfinite(sigma) or sigma <= _FULL_AT:
        return _MAX_LEV
    if sigma <= _HALF_AT:
        return _STEP
    return 0.0


class Strategy:
    WANTS_EXTENDED_HOURS = False

    def __init__(self):
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
        s_fast = float(rets[-_FAST:].std(ddof=1))
        s_slow = float(rets.std(ddof=1))
        sigma = s_fast if s_fast > s_slow else s_slow
        return _bucket(sigma * _ANN)
