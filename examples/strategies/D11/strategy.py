from __future__ import annotations

from collections import deque

import numpy as np

_RV_WIN = 20
_PCT_WIN = 252
_Q = 0.80
_CONFIRM = 2
_LONG = 1.0
_CLOSES = _RV_WIN + _PCT_WIN
_RTH_LAST = 959
_SHORT_LAST = 779


def _rv_and_threshold(closes):
    px = np.asarray(closes, dtype=np.float64)
    rets = px[1:] / px[:-1] - 1.0
    windows = np.lib.stride_tricks.sliding_window_view(rets, _RV_WIN)
    rvs = windows.std(axis=1, ddof=1) * np.sqrt(252.0)
    rv = float(rvs[-1])
    thr = float(np.quantile(rvs, _Q))
    return rv, thr


class Strategy:
    WANTS_EXTENDED_HOURS = False

    def __init__(self):
        self.closes = deque(maxlen=_CLOSES)
        self.prev_flag = None
        self.streak = 0
        self.target = 0.0

    def on_bar(self, bar, state):
        try:
            minute = bar["minute"]
            if minute != _RTH_LAST and minute != _SHORT_LAST:
                return None
            short = bar["short_session"]
            if minute == _SHORT_LAST:
                if not short:
                    return None
            elif short:
                return None
            px = float(bar["close"])
            if not np.isfinite(px) or px <= 0.0:
                return None
            self.closes.append(px)
            if len(self.closes) < _CLOSES:
                return 0.0
            rv, thr = _rv_and_threshold(self.closes)
            if not np.isfinite(rv) or not np.isfinite(thr) or thr <= 0.0:
                return self.target
            want_long = rv < thr
            if self.prev_flag is None or want_long != self.prev_flag:
                self.streak = 1
                self.prev_flag = want_long
            else:
                self.streak += 1
            desired = _LONG if want_long else 0.0
            if desired != self.target and self.streak >= _CONFIRM:
                self.target = desired
            return self.target
        except Exception:
            return None

    def on_day_end(self, summary):
        return None
