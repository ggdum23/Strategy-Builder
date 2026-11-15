import math


class Strategy:
    WANTS_EXTENDED_HOURS = False

    LAM = 0.94
    SEED = 20
    TGT_VOL = 0.18
    VOL_FLOOR = 0.05
    STEP = 0.25

    def __init__(self):
        self.cur_day = None
        self.today_close = None
        self.committed = False
        self.prev_close = None
        self.var = None
        self.buf = []
        self.current = 0.0
        self.have_signal = False

    def on_bar(self, bar, state):
        day = bar["day"]
        close = bar["close"]
        if close is None or close != close or close <= 0:
            return None
        if self.cur_day is None:
            self.cur_day = day
        if day != self.cur_day:
            late = None
            if not self.committed and self.today_close is not None:
                self._commit(self.today_close)
                if self.have_signal:
                    late = self.current
            self.cur_day = day
            self.today_close = close
            self.committed = False
            if late is not None:
                return late
        self.today_close = close
        short = bar["short_session"]
        minute = bar["minute"]
        is_last = (short and minute == 779) or ((not short) and minute == 959)
        if not is_last:
            return None
        self._commit(close)
        self.committed = True
        if not self.have_signal:
            return 0.0
        return self.current

    def _commit(self, close):
        if self.prev_close is not None and self.prev_close > 0 and close > 0:
            self._update(close / self.prev_close - 1.0)
        self.prev_close = close

    def _update(self, ret):
        sq = ret * ret
        if self.var is None:
            self.buf.append(sq)
            if len(self.buf) < self.SEED:
                return
            self.var = sum(self.buf) / float(self.SEED)
        else:
            self.var = self.LAM * self.var + (1.0 - self.LAM) * sq
        vol = math.sqrt(self.var * 252.0) if self.var > 0 else 0.0
        sig = vol if vol > self.VOL_FLOOR else self.VOL_FLOOR
        raw = self.TGT_VOL / sig
        if raw > 1.0:
            raw = 1.0
        elif raw < 0.0:
            raw = 0.0
        self.have_signal = True
        step = self.STEP
        if abs(raw - self.current) + 1e-15 >= step:
            q = math.floor(raw / step + 0.5) * step
            if q > 1.0:
                q = 1.0
            elif q < 0.0:
                q = 0.0
            if abs(q - self.current) < 1e-12:
                if raw > self.current:
                    q = min(1.0, self.current + step)
                else:
                    q = max(0.0, self.current - step)
            self.current = q

    def on_day_end(self, summary):
        return None
