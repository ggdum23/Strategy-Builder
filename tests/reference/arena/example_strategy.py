"""Interface-only example. It is NOT a recommended strategy; it only shows the contract.

Rules (illustrative): on regular-session bars, keep an exponential moving average of closes with a
~10-session span (3900 one-minute bars) and decide ONCE per session, on the bar at 15:50 (or 12:50
on shortened sessions): target +1.0 if the close is above the average, else 0.0. Otherwise return
None (= keep the current target, no order).
"""


class Strategy:
    WANTS_EXTENDED_HOURS = False  # True -> also receive pre/post-session bars

    def __init__(self):
        self.alpha = 2.0 / (3900 + 1)
        self.ema = None

    def on_bar(self, bar, state):
        c = bar["close"]
        self.ema = c if self.ema is None else self.ema + self.alpha * (c - self.ema)
        decision_minute = 770 if bar["short_session"] else 950  # 12:50 / 15:50
        if bar["minute"] != decision_minute:
            return None
        return 1.0 if c > self.ema else 0.0

    def on_day_end(self, summary):  # optional
        pass
