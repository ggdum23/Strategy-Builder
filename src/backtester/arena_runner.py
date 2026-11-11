"""Port of the reference pack runner (``run_backtest.py``).

The windows here are the anonymized QQQ pack cuts: design days 0..2999,
validation 3000..3795 with a 504-session replay that rescales and keeps
exposure. Named local runs and ``python -m backtester walkforward`` do not
use these cuts and do not default to them.

Importing this module does not change ``sys.path`` and does not read a pack.
The verbatim reference file is ``tests/reference/arena/run_backtest.py``.
"""

from __future__ import annotations

import argparse
import json
import os
import time

import pandas as pd

from . import engine_arena as B
from .strategy_loader import load_strategy_class


def run_window(strategy_cls, window, bars_cache=None):
    """Same window filter and reset as the reference runner."""
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
    elapsed = time.time() - t0
    metrics = B.metrics(res)
    metrics["runtime_seconds"] = round(elapsed, 2)
    metrics["window"] = window
    return res, metrics


def write(res, metrics, prefix):
    with open(prefix + "_metrics.json", "w", encoding="utf-8") as handle:
        json.dump(metrics, handle, indent=1, default=float)
    pd.DataFrame(
        res["daily"], columns=["day", "equity", "position", "shares", "close"]
    ).to_csv(prefix + "_daily.csv", index=False)
    pd.DataFrame(
        res["fills"],
        columns=["day", "minute", "session", "shares_delta", "open", "fill_price", "fees", "shares_after"],
    ).to_csv(prefix + "_fills.csv", index=False)
    pd.DataFrame(res["episodes"]).to_csv(prefix + "_trades.csv", index=False)


KEYS = [
    "total_return",
    "cagr",
    "ann_vol",
    "sharpe",
    "sortino",
    "max_drawdown",
    "max_dd_duration_sessions",
    "calmar",
    "round_trips",
    "hit_rate",
    "turnover_per_year",
    "fees_total",
    "borrow_cost",
    "avg_abs_exposure_bars",
    "final_equity",
]


def main(argv=None):
    """Pack-window CLI. Requires the anonymized bar pack next to the engine data dir."""
    parser = argparse.ArgumentParser(prog="backtester.arena_runner")
    parser.add_argument("--strategy", required=True)
    parser.add_argument("--window", choices=list(B.WINDOWS), default="full")
    parser.add_argument("--out", default=None)
    parser.add_argument("--label", default=None)
    parser.add_argument("--no-benchmark", action="store_true")
    args = parser.parse_args(argv)
    out = args.out or os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
    os.makedirs(out, exist_ok=True)
    label = args.label or os.path.splitext(os.path.basename(args.strategy))[0]
    strategy_cls = load_strategy_class(args.strategy)
    a, z = B.WINDOWS[args.window]
    first = max(0, a - B.VALIDATION_WARMUP_DAYS) if args.window == "validation" else a
    t0 = time.time()
    bars = B.load_bars(first, z)
    print(f"loaded {len(bars):,} bars (days {first}..{z}) in {time.time() - t0:.1f}s")
    res, metrics = run_window(strategy_cls, args.window, bars)
    write(res, metrics, os.path.join(out, f"{label}_{args.window}"))
    summary = {"strategy": {key: metrics.get(key) for key in KEYS}}
    if not args.no_benchmark:
        bench_res, bench_metrics = run_window(B.BuyAndHold, args.window, bars)
        write(bench_res, bench_metrics, os.path.join(out, f"buy_and_hold_{args.window}"))
        summary["buy_and_hold"] = {key: bench_metrics.get(key) for key in KEYS}
    with open(os.path.join(out, f"{label}_{args.window}_summary.json"), "w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=1, default=float)
    for name, values in summary.items():
        parts = []
        for key in KEYS:
            value = values[key]
            if isinstance(value, (int, float)):
                parts.append(f"{key}={value:.4g}")
            else:
                parts.append(f"{key}={value}")
        print(f"[{args.window}] {name:13s} " + " ".join(parts))
    print(f"strategy runtime {metrics['runtime_seconds']}s")
    return 0
