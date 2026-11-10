"""Slow trend, decided near the close. Long or flat. No shorts, no leverage.

Once per regular session, on the bar that starts 31 minutes before the session
ends (15:29 on a full session, 12:29 on a shortened session), compare that
bar's close with the closes 60, 120 and 252 sessions ago. If at least two of
the available comparisons are up, and at least two horizons are available,
target +1.0 (fully invested). Otherwise target 0.0 (flat). The engine fills
the change at the next bar's open, about 30 minutes before the close, so a
new long earns that night's open and a new flat skips it.

Completed session closes are recorded in on_day_end, after the decision, so
the live bar is never compared with itself. History is whatever bars have
been delivered (the forward run warms up before the account reset). No
look-ahead, no day-index rules, no file or network access.
"""


class Strategy:
    WANTS_EXTENDED_HOURS = False

    LOOKBACKS = (60, 120, 252)
    MIN_VOTES = 2
    EXPOSURE = 1.0
    # Minutes from the session-end clock back to the decision bar's start.
    # Full session ends at 16:00 (minute 960) -> decision at 15:29.
    # Short session ends at 13:00 (minute 780) -> decision at 12:29.
    DECISION_OFFSET = 31
    MAX_CLOSES = 320

    def __init__(self):
        self.closes = []

    def on_bar(self, bar, state):
        end = 780 if bar["short_session"] else 960
        if bar["minute"] != end - self.DECISION_OFFSET:
            return None
        px = float(bar["close"])
        if not px > 0.0:
            return 0.0
        npos = 0
        avail = 0
        n = len(self.closes)
        for L in self.LOOKBACKS:
            if n >= L:
                avail += 1
                if px > self.closes[-L]:
                    npos += 1
        if avail < self.MIN_VOTES:
            return 0.0
        return self.EXPOSURE if npos >= self.MIN_VOTES else 0.0

    def on_day_end(self, summary):
        c = summary.get("rth_close")
        if c is None:
            return
        c = float(c)
        if not c > 0.0:
            return
        self.closes.append(c)
        if len(self.closes) > self.MAX_CLOSES:
            del self.closes[: len(self.closes) - self.MAX_CLOSES]
