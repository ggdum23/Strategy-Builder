"""Volatility-scaled long with a slow mean-reversion tilt.

One decision per regular session, on the closing bar (minute 959, or 779 on a
short session). The order fills at the next session's open. Long or flat only.

Until 200 completed sessions are in hand the strategy stays flat. After that:

    rv      = sample stdev of the last 15 close-to-close returns, annualised
    base    = min(1.75, 0.20 / rv)
    dist    = close / SMA(200) - 1
    mult    = clip(1 - 5 * dist, 0.50, 1.50)
    desired = clip(base * mult, 0, 1.75)

A new target is sent only when it differs from the live target by more than
0.12. Otherwise the previous target is kept (no order, so exposure can drift).

Runtime: stdlib only. All inputs are the bars passed to on_bar / on_day_end.
"""

import math


class Strategy:
    WANTS_EXTENDED_HOURS = False

    VOL_LOOKBACK = 15
    SMA_LOOKBACK = 200
    TARGET_VOL = 0.20
    CAP = 1.75
    MR_K = 5.0
    MULT_LO = 0.50
    MULT_HI = 1.50
    BAND = 0.12
    ANN = math.sqrt(252.0)

    def __init__(self):
        self._closes = []          # completed session closes, oldest first
        self._target = 0.0         # last target sent to the engine
        self._decided = False      # already chose a target on this session
        self._close_pushed = False
        self._late_close = None    # session close captured in on_day_end if the decision bar was missed

    def on_bar(self, bar, state):
        minute = bar["minute"]
        decision_minute = 779 if bar["short_session"] else 959

        # Fallback: previous session ended without a decision bar. Record its close
        # and decide on this first bar (fills one bar later than usual).
        if self._late_close is not None:
            close = self._late_close
            self._late_close = None
            self._push_close(close)
            return self._send(self._desired())

        if minute < decision_minute:
            return None
        if self._decided:
            return None
        self._decided = True
        self._close_pushed = True
        self._push_close(bar["close"])
        return self._send(self._desired())

    def on_day_end(self, summary):
        if not self._close_pushed:
            # Decision minute never arrived. Keep the close and act next session.
            px = summary.get("rth_close")
            if px is not None:
                self._late_close = px
        self._decided = False
        self._close_pushed = False

    def _push_close(self, close):
        self._closes.append(close)
        extra = len(self._closes) - (self.SMA_LOOKBACK + 5)
        if extra > 0:
            del self._closes[:extra]

    def _desired(self):
        closes = self._closes
        need = self.SMA_LOOKBACK
        if len(closes) < need:
            return 0.0
        rv = self._realized_vol(closes)
        if rv <= 1e-8:
            base = self.CAP
        else:
            base = self.TARGET_VOL / rv
            if base > self.CAP:
                base = self.CAP
        sma = sum(closes[-self.SMA_LOOKBACK:]) / self.SMA_LOOKBACK
        dist = closes[-1] / sma - 1.0
        mult = 1.0 - self.MR_K * dist
        if mult < self.MULT_LO:
            mult = self.MULT_LO
        elif mult > self.MULT_HI:
            mult = self.MULT_HI
        desired = base * mult
        if desired < 0.0:
            return 0.0
        if desired > self.CAP:
            return self.CAP
        return desired

    def _realized_vol(self, closes):
        n = self.VOL_LOOKBACK
        # last n close-to-close returns; need n+1 prices
        window = closes[-(n + 1):]
        rets = [window[i] / window[i - 1] - 1.0 for i in range(1, len(window))]
        m = sum(rets) / n
        var = sum((x - m) * (x - m) for x in rets) / (n - 1)
        if var < 0.0:
            var = 0.0
        return math.sqrt(var) * self.ANN

    def _send(self, desired):
        if abs(desired - self._target) <= self.BAND:
            return None
        self._target = desired
        return desired
