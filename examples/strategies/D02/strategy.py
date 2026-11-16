
CORE = 1.0
BOOST = 2.0
GAP_THRESHOLD = 0.012
HOLD_MINUTES = 30
OPEN_MINUTE = 570
EXIT_MINUTE = OPEN_MINUTE + HOLD_MINUTES


class Strategy:
    WANTS_EXTENDED_HOURS = False

    def __init__(self):
        self.prev_close = None
        self.day = None
        self.checked = False
        self.boosting = False
        self.faults = 0

    def on_bar(self, bar, state):
        try:
            return self._on_bar(bar, state)
        except Exception:
            self.faults += 1
            return None

    def on_day_end(self, summary):
        try:
            px = summary.get("rth_close")
            if px is not None and px > 0:
                self.prev_close = float(px)
        except Exception:
            self.faults += 1

    def _on_bar(self, bar, state):
        minute = int(bar["minute"])
        day = int(bar["day"])
        short = int(bar["short_session"]) == 1
        last = 779 if short else 959

        if day != self.day:
            self.day = day
            self.checked = False
            self.boosting = False

        if self.prev_close is None or self.prev_close <= 0:
            if abs(state["target"] - CORE) > 1e-12:
                return CORE
            return None

        if not self.checked:
            self.checked = True
            if minute == OPEN_MINUTE and EXIT_MINUTE < last:
                opened = float(bar["open"])
                if opened > 0:
                    gap = opened / self.prev_close - 1.0
                    if gap <= -GAP_THRESHOLD:
                        self.boosting = True
                        return BOOST

        if self.boosting and (minute >= EXIT_MINUTE or minute == last):
            self.boosting = False
            return CORE

        if not self.boosting and abs(state["target"] - CORE) > 1e-12:
            return CORE
        return None
