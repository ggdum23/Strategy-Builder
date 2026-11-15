
ENTER_DEV = -0.002
EXIT_DEV = 0.004
LONG_SIZE = 1.0
LAST_MINUTE = 959
LAST_MINUTE_SHORT = 779


class Strategy:
    WANTS_EXTENDED_HOURS = False

    def __init__(self):
        self._day = None
        self._pv = 0.0
        self._vol = 0.0
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
