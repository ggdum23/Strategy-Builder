"""Long-only inverse-volatility overlay for ASSET.

Decision frequency: once per regular session, on the last RTH bar (15:59, or
12:59 on a shortened session). The order fills at the next delivered bar's
open, which is the next session's open. No extended-hours trading.

After 63 close-to-close returns exist, the target fraction of equity is

    raw = 0.22 / max(0.05, 0.5 * (rv21 + rv63))
    target = clip(round_half_up(raw, step=0.25), 0, 1.5)

where rv21 and rv63 are the annualized sample standard deviations of the last
21 and 63 close-to-close returns. The position is always long after warm-up
(it never shorts). Quantizing to quarter-turns is what keeps turnover low:
an unchanged bucket does not rebalance, and price drift is left alone.

The 0.22 vol budget, 63/21 blend, 1.5 cap and 0.25 step are frozen constants
from the design-window study. Nothing here reads files, the clock, or the network.
"""
import math


class Strategy:
    WANTS_EXTENDED_HOURS = False

    TARGET_VOL = 0.22
    VOL_FLOOR = 0.05
    CAP = 1.5
    STEP = 0.25
    FAST = 21
    SLOW = 63
    # Last regular-session bar start: 15:59, or 12:59 when the session ends at 13:00.
    LAST_MINUTE = 15 * 60 + 59
    LAST_MINUTE_SHORT = 12 * 60 + 59

    def __init__(self):
        self.closes = []
        self.cur_day = None
        self.cur_close = None
        self.finalized = False

    def on_bar(self, bar, state):
        try:
            return self._on_bar(bar, state)
        except Exception:
            return None

    def _on_bar(self, bar, state):
        day = int(bar["day"])
        close = float(bar["close"])
        minute = int(bar["minute"])
        if not (close > 0.0):
            return None
        short = int(bar["short_session"])
        if self.cur_day is None:
            self.cur_day = day
            self.cur_close = close
            self.finalized = False
            return None
        if day != self.cur_day:
            target = None
            if not self.finalized and self.cur_close is not None and self.cur_close > 0.0:
                self._append(self.cur_close)
                self.finalized = True
                target = self._target()
            self.cur_day = day
            self.cur_close = close
            self.finalized = False
            return target
        self.cur_close = close
        decision = self.LAST_MINUTE_SHORT if short else self.LAST_MINUTE
        if minute == decision and not self.finalized:
            self._append(close)
            self.finalized = True
            return self._target()
        return None

    def _append(self, close):
        self.closes.append(close)
        # 64 closes cover the 63-return window with one extra bar of slack.
        if len(self.closes) > 80:
            del self.closes[:-80]

    def _sample_std(self, n_returns):
        """Sample std (ddof=1) of the last n_returns close-to-close returns."""
        need = n_returns + 1
        w = self.closes[-need:]
        if len(w) < need:
            return None
        n = n_returns
        s = 0.0
        s2 = 0.0
        prev = w[0]
        for i in range(1, need):
            px = w[i]
            r = px / prev - 1.0
            prev = px
            s += r
            s2 += r * r
        var = (s2 - (s * s) / n) / (n - 1)
        if var < 0.0:
            var = 0.0
        return math.sqrt(var)

    def _target(self):
        if len(self.closes) < self.SLOW + 1:
            return 0.0
        fast = self._sample_std(self.FAST)
        slow = self._sample_std(self.SLOW)
        if fast is None or slow is None:
            return 0.0
        rv = 0.5 * (fast + slow) * math.sqrt(252.0)
        if rv < self.VOL_FLOOR:
            rv = self.VOL_FLOOR
        raw = self.TARGET_VOL / rv
        if raw > self.CAP:
            raw = self.CAP
        if raw < 0.0:
            raw = 0.0
        ticks = int(math.floor(raw / self.STEP + 0.5))
        max_ticks = int(round(self.CAP / self.STEP))
        if ticks < 0:
            ticks = 0
        elif ticks > max_ticks:
            ticks = max_ticks
        return ticks * self.STEP
