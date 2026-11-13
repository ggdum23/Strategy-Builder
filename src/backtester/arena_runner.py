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
import sys
import time

import pandas as pd

from . import engine_arena as B


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
    """Pack-window CLI. A strategy file is not loaded in this process.

    ``run_window`` still accepts an in-memory class. This entry point only
    receives a path, and loading that path would run designer code here.
    """
    parser = argparse.ArgumentParser(prog="backtester.arena_runner")
    parser.add_argument("--strategy", required=True)
    parser.add_argument("--window", choices=list(B.WINDOWS), default="full")
    parser.add_argument("--out", default=None)
    parser.add_argument("--label", default=None)
    parser.add_argument("--no-benchmark", action="store_true")
    parser.parse_args(argv)
    print(
        "error: arena_runner does not load a strategy file. Use python -m backtester.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
