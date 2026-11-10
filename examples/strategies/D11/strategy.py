"""Calm-regime equity overlay for ASSET.

Once per regular session, on the last RTH bar, classify the market into two
regimes from trailing realized volatility and set the target exposure:

  calm  -> +1.0 (fully invested, long)
  hot   ->  0.0 (flat)

Regime rule (known at that bar's close; no same-bar fill):
  r_t        = close_t / close_{t-1} - 1
  RV20_t     = sample_stdev(r_{t-19..t}) * sqrt(252)
  thresh_t   = 80th percentile of RV20 over the last 252 sessions
  raw long   = RV20_t < thresh_t
A regime change is acted on only after it has persisted for 2 consecutive
sessions, so one-session vol spikes do not trade. The order fills at the next
delivered bar's open (the next session's open).

All quantities use only closes already received. History kept in memory is the
last 272 session closes, which is exactly enough for 252 overlapping 20-day
vols. No fitted statistical model is evaluated at runtime; the percentile
threshold is recomputed from the bars seen so far.
"""
from __future__ import annotations

from collections import deque

import numpy as np

# Frozen on the design window (sessions 0..2999). Do not retune out of sample.
_RV_WIN = 20
_PCT_WIN = 252
_Q = 0.80
_CONFIRM = 2
_LONG = 1.0
# 252 overlapping 20-return vols need 20 + 252 - 1 = 271 returns = 272 closes.
_CLOSES = _RV_WIN + _PCT_WIN
_RTH_LAST = 959          # 15:59 bar start; regular session ends 16:00
_SHORT_LAST = 779        # 12:59 bar start; shortened session ends 13:00


def _rv_and_threshold(closes):
    """Latest annualized 20-session vol and its trailing 252-session 80th percentile.

    `closes` has length _CLOSES. Returns (rv, threshold).
    """
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
        self.prev_flag = None  # last raw "want long" reading, or None before warmup
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
