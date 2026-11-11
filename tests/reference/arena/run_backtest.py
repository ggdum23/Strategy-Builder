"""Official runner (IMMUTABLE). Usage:
    python run_backtest.py --strategy strategy.py --window full|design|validation [--out results]
Writes results/<label>_<window>_{metrics.json,daily.csv,fills.csv,trades.csv} for the strategy AND for
buy-and-hold run through the same engine and costs, plus results/<label>_<window>_summary.json.
Windows: full = days 0..3795, design = 0..2999, validation = 3000..3795 (validation replays the
previous 504 sessions as a live warm-up, then resets the account to $1M keeping exposure).
"""
import argparse
import importlib.util
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest as B  # noqa: E402

import pandas as pd  # noqa: E402


def load_strategy(path):
    spec = importlib.util.spec_from_file_location("user_strategy", os.path.abspath(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.Strategy


def run_window(strategy_cls, window, bars_cache=None):
    a, z = B.WINDOWS[window]
    first = a
    reset_day = None
    if window == "validation":
        first = max(0, a - B.VALIDATION_WARMUP_DAYS)
        reset_day = a
    bars = bars_cache if bars_cache is not None else B.load_bars(first, z)
    bars = bars[(bars["day"] >= first) & (bars["day"] <= z)]
    t0 = time.time()
    res = B.run(strategy_cls(), bars, reset_day=reset_day)
    el = time.time() - t0
    m = B.metrics(res)
    m["runtime_seconds"] = round(el, 2)
    m["window"] = window
    return res, m


def write(res, m, prefix):
    with open(prefix + "_metrics.json", "w") as f:
        json.dump(m, f, indent=1, default=float)
    pd.DataFrame(res["daily"], columns=["day", "equity", "position", "shares", "close"]).to_csv(prefix + "_daily.csv", index=False)
    pd.DataFrame(res["fills"], columns=["day", "minute", "session", "shares_delta", "open", "fill_price", "fees", "shares_after"]).to_csv(prefix + "_fills.csv", index=False)
    pd.DataFrame(res["episodes"]).to_csv(prefix + "_trades.csv", index=False)


KEYS = ["total_return", "cagr", "ann_vol", "sharpe", "sortino", "max_drawdown", "max_dd_duration_sessions", "calmar",
        "round_trips", "hit_rate", "turnover_per_year", "fees_total", "borrow_cost", "avg_abs_exposure_bars", "final_equity"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", required=True)
    ap.add_argument("--window", choices=list(B.WINDOWS), default="full")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "results"))
    ap.add_argument("--label", default=None)
    ap.add_argument("--no-benchmark", action="store_true")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    label = args.label or os.path.splitext(os.path.basename(args.strategy))[0]
    S = load_strategy(args.strategy)
    a, z = B.WINDOWS[args.window]
    first = max(0, a - B.VALIDATION_WARMUP_DAYS) if args.window == "validation" else a
    t0 = time.time()
    bars = B.load_bars(first, z)
    print(f"loaded {len(bars):,} bars (days {first}..{z}) in {time.time() - t0:.1f}s")
    res, m = run_window(S, args.window, bars)
    write(res, m, os.path.join(args.out, f"{label}_{args.window}"))
    summary = {"strategy": {k: m.get(k) for k in KEYS}}
    if not args.no_benchmark:
        rb, mb = run_window(B.BuyAndHold, args.window, bars)
        write(rb, mb, os.path.join(args.out, f"buy_and_hold_{args.window}"))
        summary["buy_and_hold"] = {k: mb.get(k) for k in KEYS}
    with open(os.path.join(args.out, f"{label}_{args.window}_summary.json"), "w") as f:
        json.dump(summary, f, indent=1, default=float)
    for name, v in summary.items():
        print(f"[{args.window}] {name:13s} " + " ".join(f"{k}={v[k]:.4g}" if isinstance(v[k], (int, float)) else f"{k}={v[k]}" for k in KEYS))
    print(f"strategy runtime {m['runtime_seconds']}s")


if __name__ == "__main__":
    main()
