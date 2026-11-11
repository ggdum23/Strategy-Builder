"""Opening-gap absorption.

The book stays long one times equity. When the regular session opens 1.2% or more
below the previous regular close, exposure is lifted to two times equity for 30
minutes and then put back to one times. There is no short and no flat state after
the opening trade.

Decisions use only the bar that just closed and state carried forward from earlier
bars. The extra unit fills at the next bar's open (the 09:31 bar). It is removed
by a decision on the 10:00 bar, which fills on the 10:01 bar.
"""

CORE = 1.0
BOOST = 2.0
GAP_THRESHOLD = 0.012  # fade down-gaps at least this large
HOLD_MINUTES = 30
OPEN_MINUTE = 570  # 09:30, first regular bar
EXIT_MINUTE = OPEN_MINUTE + HOLD_MINUTES  # 10:00 bar; fill is the following open


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

        # No prior close yet (first session seen): get long and wait.
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

        # Scheduled exit, or a last-bar safety exit so a boost is not held overnight.
        if self.boosting and (minute >= EXIT_MINUTE or minute == last):
            self.boosting = False
            return CORE

        if not self.boosting and abs(state["target"] - CORE) > 1e-12:
            return CORE
        return None
