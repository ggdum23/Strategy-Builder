"""Engine/cost unit tests. Run: python -m unittest discover -s tests   (or: pytest tests)"""
import hashlib
import json
import math
import os
import sys
import unittest

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import backtest as B  # noqa: E402
import costs as C  # noqa: E402

SLIP = C.SLIPPAGE_BPS * 1e-4


def make_bars(days=3, rth_minutes=(570, 571, 572), pre=(), post=(), price=lambda i: 100.0 + i, short_day=None):
    rows = []
    i = 0
    for d in range(days):
        for sess, mins in (("pre", pre), ("rth", rth_minutes), ("post", post)):
            for m in mins:
                p = price(i)
                rows.append({"day": d, "weekday": d % 5, "minute": m, "time": f"{m // 60:02d}:{m % 60:02d}", "session": sess,
                             "open": p, "high": p + 0.5, "low": p - 0.5, "close": p + 0.25, "volume": 1000, "wap": p,
                             "count": 10, "filled": 0, "short_session": int(short_day == d)})
                i += 1
    return pd.DataFrame(rows)


class Scripted:
    """Returns targets from a dict keyed by (day, minute); records what it saw."""
    WANTS_EXTENDED_HOURS = False

    def __init__(self, plan, ext=False):
        self.plan = plan
        self.WANTS_EXTENDED_HOURS = ext
        self.seen = []

    def on_bar(self, bar, state):
        self.seen.append((bar["day"], bar["minute"], bar["session"]))
        return self.plan.get((bar["day"], bar["minute"]))


class Always:
    WANTS_EXTENDED_HOURS = False

    def __init__(self, t):
        self.t = t

    def on_bar(self, bar, state):
        return self.t


class TestFills(unittest.TestCase):
    def test_next_bar_open_fill(self):
        bars = make_bars()
        res = B.run(Scripted({(0, 570): 1.0}), bars)
        self.assertEqual(len(res["fills"]), 1)
        day, minute, sess, delta, o, px, fee, sh = res["fills"][0]
        self.assertEqual((day, minute), (0, 571))           # decision on 570 -> fill at 571 open
        self.assertEqual(o, 101.0)
        self.assertAlmostEqual(px, 101.0 * (1 + SLIP))
        self.assertEqual(delta, int(1e6 / 101.0))

    def test_last_bar_fills_next_session_first_delivered_bar(self):
        bars = make_bars(pre=(500,), post=(1000,))
        res = B.run(Scripted({(0, 572): 1.0}), bars)          # rth-only: skips post and next pre
        self.assertEqual(res["fills"][0][:3], (1, 570, "rth"))
        res = B.run(Scripted({(0, 572): 1.0}, ext=True), bars)  # extended: next delivered bar is post
        self.assertEqual(res["fills"][0][:3], (0, 1000, "post"))

    def test_no_same_bar_fill_and_last_bar_unfilled(self):
        bars = make_bars(days=1)
        res = B.run(Scripted({(0, 572): 1.0}), bars)
        self.assertEqual(res["fills"], [])

    def test_whole_share_truncation(self):
        bars = make_bars(price=lambda i: 33.0)
        res = B.run(Scripted({(0, 570): 1.0}), bars)
        self.assertEqual(res["fills"][0][3], 30303)            # trunc(1e6/33)=30303
        res = B.run(Scripted({(0, 570): -0.5}), bars)
        self.assertEqual(res["fills"][0][3], -15151)           # trunc toward zero
        self.assertIsInstance(res["fills"][0][3], int)

    def test_no_drift_rebalance(self):
        bars = make_bars(days=5, price=lambda i: 100.0 * (1.01 ** i))
        res = B.run(Always(1.0), bars)
        self.assertEqual(res["n_fills"], 1)
        res = B.run(Always(None), bars)
        self.assertEqual(res["n_fills"], 0)

    def test_clamp(self):
        bars = make_bars(price=lambda i: 50.0)
        r1 = B.run(Scripted({(0, 570): 5.0}), bars)
        r2 = B.run(Scripted({(0, 570): 2.0}), bars)
        self.assertEqual(r1["fills"][0][3], r2["fills"][0][3])
        self.assertEqual(r1["fills"][0][3], 40000)
        r3 = B.run(Scripted({(0, 570): -9.0}), bars)
        self.assertEqual(r3["fills"][0][3], -40000)

    def test_target_change_and_back(self):
        bars = make_bars(price=lambda i: 50.0)
        res = B.run(Scripted({(0, 570): 1.0, (0, 571): 0.0, (1, 570): 0.0}), bars)
        self.assertEqual([f[3] for f in res["fills"]], [20000, -20000])
        self.assertEqual(len(res["episodes"]), 1)

    def test_non_finite_raises(self):
        with self.assertRaises(ValueError):
            B.run(Scripted({(0, 570): float("nan")}), make_bars())

    def test_accounting_identity(self):
        bars = make_bars(days=4, price=lambda i: 100 + 3 * math.sin(i))
        plan = {(0, 570): 1.5, (1, 571): -1.0, (2, 570): 0.3, (3, 570): 0.0}
        res = B.run(Scripted(plan), bars)
        cash = 1e6
        sh = 0
        for (d, m, s, delta, o, px, fee, after) in res["fills"]:
            cash -= delta * px + fee
            sh = after
        cash -= res["borrow"]
        final = res["daily"][-1][1]
        self.assertAlmostEqual(cash + sh * res["daily"][-1][4], final, places=6)
        self.assertGreater(res["borrow"], 0)

    def test_borrow_charge(self):
        bars = make_bars(days=3, price=lambda i: 100.0)
        res = B.run(Scripted({(0, 570): -1.0}), bars)
        sh = res["fills"][0][3]
        # short held over the close of day 0, 1, 2
        exp = 3 * C.daily_borrow(-sh * 100.25)
        self.assertAlmostEqual(res["borrow"], exp, places=6)

    def test_reset_keeps_exposure(self):
        bars = make_bars(days=4, price=lambda i: 100.0 * (1.02 ** i))
        res = B.run(Always(1.0), bars, reset_day=2)
        self.assertEqual(res["daily"][0][0], 2)
        self.assertAlmostEqual(res["daily"][0][2], 1.0, delta=0.01)
        self.assertEqual(res["n_fills"], 0)
        m = B.metrics(res)
        self.assertEqual(m["first_day"], 2)


class TestCosts(unittest.TestCase):
    def test_tier_marginal(self):
        self.assertAlmostEqual(C.tiered_commission(100, 0), 0.35)
        self.assertAlmostEqual(C.tiered_commission(200_000, 200_000), 100_000 * 0.0035 + 100_000 * 0.0020)
        self.assertAlmostEqual(C.tiered_commission(10, 3_000_000), 10 * 0.0015)
        self.assertAlmostEqual(C.tiered_commission(10, 20_000_000), 10 * 0.0010)
        self.assertAlmostEqual(C.tiered_commission(10, 100_000_000), 10 * 0.0005)

    def test_minimum_and_cap(self):
        f = C.fill_costs(10, 100.0, 0)
        self.assertAlmostEqual(f["commission"], 0.35)       # min
        f = C.fill_costs(100, 0.10, 0)                       # value $10 -> cap 1% = $0.10
        self.assertAlmostEqual(f["commission"], 0.10)
        self.assertEqual(f["tier_shares"], 0.0)              # capped orders don't count toward tiers
        self.assertAlmostEqual(f["clearing"], min(100 * C.CLEARING_PER_SHARE, 0.005 * 10))

    def test_sell_only_fees(self):
        b = C.fill_costs(1000, 50.0, 0)
        s = C.fill_costs(-1000, 50.0, 0)
        self.assertEqual(b["sec"], 0.0); self.assertEqual(b["taf"], 0.0)
        self.assertAlmostEqual(s["sec"], C.SEC_FEE_RATE * 50_000)
        self.assertAlmostEqual(s["taf"], min(1000 * C.TAF_PER_SHARE, C.TAF_MAX))
        big = C.fill_costs(-1_000_000, 50.0, 0)
        self.assertAlmostEqual(big["taf"], C.TAF_MAX)
        self.assertEqual(C.fill_costs(-1000, 50.0, 0, taf_active=False)["taf"], 0.0)

    def test_share_scale(self):
        a = C.fill_costs(20_000, 50.0, 0, share_scale=0.5)
        b = C.fill_costs(10_000, 100.0, 0)
        for k in ("commission", "exchange", "clearing", "cat", "total"):
            self.assertAlmostEqual(a[k], b[k])

    def test_monthly_tier_in_engine(self):
        # 3 fills in the same 21-session 'month': the later fills get cheaper marginal rates
        bars = make_bars(days=3, price=lambda i: 10.0)
        res = B.run(Scripted({(0, 570): 2.0, (0, 571): -2.0, (1, 570): 2.0}), bars)
        fees = [f[6] for f in res["fills"]]
        self.assertGreater(fees[0], 0)
        self.assertLess(fees[2] / abs(res["fills"][2][3]), fees[0] / abs(res["fills"][0][3]))

    def test_slippage(self):
        self.assertAlmostEqual(C.slipped_price(100.0, 5), 100.0 * (1 + SLIP))
        self.assertAlmostEqual(C.slipped_price(100.0, -5), 100.0 * (1 - SLIP))


class TestPack(unittest.TestCase):
    def test_manifest_hashes(self):
        with open(os.path.join(B.DATA_DIR, "MANIFEST.json")) as fh:
            man = json.load(fh)
        for name, info in man["files"].items():
            p = os.path.join(B.DATA_DIR, name)
            self.assertTrue(os.path.exists(p), name)
            with open(p, "rb") as fh:
                self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), info["sha256"], name)

    def test_metrics_buy_and_hold_synthetic(self):
        bars = make_bars(days=10, price=lambda i: 100.0)
        res = B.run(B.BuyAndHold(), bars)
        m = B.metrics(res)
        sh = 10_000  # trunc(1e6 / 100) at the second bar's open
        px = 100.0 * (1 + SLIP)
        fee = C.fill_costs(sh, px, 0)["total"]
        expect = 1e6 - sh * px - fee + sh * 100.25  # marked at the last close (open + 0.25)
        self.assertAlmostEqual(m["final_equity"], expect, places=6)
        self.assertEqual(m["n_fills"], 1)
        self.assertEqual(m["round_trips"], 0)
        self.assertEqual(m["sessions"], 10)


if __name__ == "__main__":
    unittest.main()
