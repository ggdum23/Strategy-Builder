

class Strategy:
    WANTS_EXTENDED_HOURS = False

    ATR_WINDOW = 20
    RANK_WINDOW = 252
    COMPRESSED_ENTER = 0.25
    COMPRESSED_EXIT = 0.40
    EXPANDED_ENTER = 0.70
    EXPANDED_EXIT = 0.55
    EXPOSURE = {"compressed": 1.5, "neutral": 1.0, "expanded": 0.0}

    def __init__(self):
        self.day = None
        self.sess_high = None
        self.sess_low = None
        self.sess_close = None
        self.prev_close = None
        self.trs = []
        self.atr_pcts = []
        self.regime = "neutral"
        self.target = 0.0

    def on_bar(self, bar, state):
        try:
            high = float(bar["high"])
            low = float(bar["low"])
            close = float(bar["close"])
            minute = int(bar["minute"])
            day = int(bar["day"])
            short = int(bar["short_session"])
        except (KeyError, TypeError, ValueError):
            return None
        if not _finite(high) or not _finite(low) or not _finite(close) or close <= 0.0 or high < low:
            return None

        if self.day != day:
            self.day = day
            self.sess_high = high
            self.sess_low = low
            self.sess_close = close
        else:
            if high > self.sess_high:
                self.sess_high = high
            if low < self.sess_low:
                self.sess_low = low
            self.sess_close = close

        if minute != (779 if short else 959):
            return None
        return self._on_session_close()

    def _on_session_close(self):
        high = self.sess_high
        low = self.sess_low
        close = self.sess_close
        prev = self.prev_close
        tr = high - low
        if prev is not None and prev > 0.0:
            up = high - prev
            if up < 0.0:
                up = -up
            down = low - prev
            if down < 0.0:
                down = -down
            if up > tr:
                tr = up
            if down > tr:
                tr = down
        self.prev_close = close

        self.trs.append(tr)
        if len(self.trs) > self.ATR_WINDOW:
            del self.trs[0]
        if len(self.trs) < self.ATR_WINDOW:
            return None

        atr = 0.0
        for value in self.trs:
            atr += value
        atr /= float(self.ATR_WINDOW)
        self.atr_pcts.append(atr / close)
        if len(self.atr_pcts) > self.RANK_WINDOW:
            del self.atr_pcts[0]
        if len(self.atr_pcts) < self.RANK_WINDOW:
            return None

        current = self.atr_pcts[-1]
        n_le = 0
        for value in self.atr_pcts:
            if value <= current:
                n_le += 1
        rank = n_le / float(self.RANK_WINDOW)

        regime = self.regime
        if regime == "compressed" and rank >= self.COMPRESSED_EXIT:
            regime = "neutral"
        elif regime == "expanded" and rank <= self.EXPANDED_EXIT:
            regime = "neutral"
        if regime == "neutral":
            if rank <= self.COMPRESSED_ENTER:
                regime = "compressed"
            elif rank >= self.EXPANDED_ENTER:
                regime = "expanded"
        self.regime = regime

        target = self.EXPOSURE[regime]
        if target == self.target:
            return None
        self.target = target
        return target


def _finite(x):
    return x == x and x != float("inf") and x != float("-inf")
