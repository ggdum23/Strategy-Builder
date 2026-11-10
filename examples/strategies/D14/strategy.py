"""Long-only volatility targeting for ASSET.

Once per regular session, at the last RTH bar, forecast next-session volatility
with a fast EWMA of daily close-to-close log returns and set gross exposure to
0.18 / sigma, capped at 2. Weights are quantized to 1/4 and traded only when
the new weight differs from the live target by at least 1/2. The order fills
at the next session's open (engine convention). No shorts.

All constants were selected on design sessions 0..1999. Runtime inputs are the
bars passed to on_bar; nothing is read from disk or the network.
"""
import math


class Strategy:
    WANTS_EXTENDED_HOURS = False

    # Forecast and sizing. 0.90 half-life is about 6.6 sessions.
    LAM = 0.90
    TARGET_VOL = 0.18
    PRIOR_VOL = 0.20
    MAX_LEV = 2.0
    STEP = 0.25
    BAND = 0.50

    def __init__(self):
        self.var = (self.PRIOR_VOL ** 2) / 252.0
        self.prev_close = None
        self.held = None  # last target actually sent

    def _quantize(self, raw):
        if raw < 0.0:
            raw = 0.0
        elif raw > self.MAX_LEV:
            raw = self.MAX_LEV
        ticks = int(round(raw / self.STEP))
        if ticks < 0:
            ticks = 0
        elif ticks > 8:
            ticks = 8
        return ticks * self.STEP

    def _target_from_var(self):
        sig = math.sqrt(self.var * 252.0) if self.var > 0.0 else self.PRIOR_VOL
        if not math.isfinite(sig) or sig <= 0.0:
            sig = self.PRIOR_VOL
        return self._quantize(self.TARGET_VOL / sig)

    def on_bar(self, bar, state):
        try:
            minute = int(bar["minute"])
            close = float(bar["close"])
            short = int(bar["short_session"])
        except (KeyError, TypeError, ValueError):
            return None
        if not math.isfinite(close) or close <= 0.0:
            return None

        # One decision per session, on the last regular-session bar
        # (15:59, or 12:59 when the session ends at 13:00). The fill is the
        # next session's open. Intraday bars do not change the target.
        if minute != (779 if short else 959):
            return None

        if self.prev_close is not None and self.prev_close > 0.0:
            lr = math.log(close / self.prev_close)
            if math.isfinite(lr):
                self.var = self.LAM * self.var + (1.0 - self.LAM) * lr * lr
                if not math.isfinite(self.var) or self.var < 0.0:
                    self.var = (self.PRIOR_VOL ** 2) / 252.0
        self.prev_close = close

        q = self._target_from_var()
        # First close always publishes the prior-vol weight. Later closes
        # publish only when the quantized weight has moved by the band.
        if self.held is None or abs(q - self.held) >= self.BAND - 1e-12:
            self.held = q
            return q
        return None
