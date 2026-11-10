"""VWAP-dislocation hysteresis, long or flat.

Once per regular session, on the last bar (15:59, or 12:59 when the session is short),
compare the close with the session VWAP built from bars already seen:

    session_vwap = sum(wap * volume) / sum(volume)
    dev          = (close - session_vwap) / close

A close at least 20 bps below VWAP means the session finished cheap relative to where
volume traded. Go long one times equity. Hold until a later session closes at least
40 bps above its VWAP, then flatten. Otherwise keep the current target. The engine
fills a changed target at the next bar's open (here, the next session's open).

No shorts, no leverage above 1, no extended-hours bars. Constants below were fixed on
the design window; nothing is fit at runtime.
"""

ENTER_DEV = -0.002
EXIT_DEV = 0.004
LONG_SIZE = 1.0
# Bar-start minute of the last regular-session bar.
LAST_MINUTE = 959          # 15:59
LAST_MINUTE_SHORT = 779    # 12:59


class Strategy:
    WANTS_EXTENDED_HOURS = False

    def __init__(self):
        self._day = None
        self._pv = 0.0   # sum of wap * volume
        self._vol = 0.0  # sum of volume
        self._long = False

    def on_bar(self, bar, state):
        day = bar["day"]
        if day != self._day:
            self._day = day
            self._pv = 0.0
            self._vol = 0.0

        vol = bar["volume"]
        if vol:
            self._pv += bar["wap"] * vol
            self._vol += vol

        minute = LAST_MINUTE_SHORT if bar["short_session"] else LAST_MINUTE
        if bar["minute"] != minute:
            return None
        if self._vol <= 0.0:
            return None
        close = bar["close"]
        if not close or close <= 0.0:
            return None

        dev = (close - self._pv / self._vol) / close
        if not self._long and dev <= ENTER_DEV:
            self._long = True
        elif self._long and dev >= EXIT_DEV:
            self._long = False
        return LONG_SIZE if self._long else 0.0
