"""Tiny filled files and report directories. Not market data."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd


def write_filled(root, symbol: str = "QQQ", *, gap: bool = False) -> Path:
    """Two January 2024 sessions, a handful of minutes each, under ``root``."""
    et = ZoneInfo("America/New_York")
    rows = []
    price = 100.0
    for day in (2, 3):
        slots = [(8, 0, "pre"), (8, 1, "pre")]
        regular = [(9, 30, "rth"), (9, 31, "rth"), (9, 32, "rth"), (9, 33, "rth")]
        if gap:
            regular = [(9, 30, "rth"), (9, 31, "rth"), (9, 33, "rth")]
        slots.extend(regular)
        slots.extend([(16, 0, "post"), (16, 1, "post")])
        for hour, minute, session in slots:
            stamp = datetime(2024, 1, day, hour, minute, tzinfo=et)
            rows.append(
                {
                    "timestamp_et": stamp.isoformat(),
                    "epoch_utc": int(stamp.timestamp()),
                    "open": price,
                    "high": round(price + 0.01, 2),
                    "low": price,
                    "close": round(price + 0.01, 2),
                    "volume": 1000,
                    "wap": round(price + 0.01, 2),
                    "count": 5,
                    "filled": 0,
                    "session": session,
                }
            )
            price = round(price + 0.01, 2)
    folder = Path(root)
    folder.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(rows)
    frame.to_csv(folder / f"{symbol}_1min_filled_2024-01.csv.gz", index=False)
    return folder


def write_strategy(path: Path, target: float = 1.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "class Strategy:\n"
        "    def on_bar(self, bar, state):\n"
        f"        return {target}\n",
        encoding="utf-8",
    )
    return path


def _equity(initial: float, values: list[float], start: str = "2024-01-02") -> str:
    day = datetime.fromisoformat(start)
    lines = ["day,date,equity,position,shares,close"]
    for index, equity in enumerate(values):
        lines.append(
            f"{index},{day.date().isoformat()},{equity},{1.0 if equity else 0},{10},{100 + index}"
        )
        day += timedelta(days=1)
    return "\n".join(lines) + "\n"


def write_book(directory: Path, metrics: dict, *, equity: list[float] | None = None, benchmark: bool = True) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
    series = equity or [1_000_000, 1_010_000, 1_005_000, 1_040_000]
    (directory / "equity_curve.csv").write_text(_equity(metrics.get("initial_equity", 1_000_000), series), encoding="utf-8")
    (directory / "fills.csv").write_text(
        "day,date,minute,session,shares_delta,open,fill_price,fees,shares_after\n"
        "0,2024-01-02,570,rth,10,100,100.01,0.35,10\n"
        "1,2024-01-03,570,rth,-4,101,100.99,0.35,6\n",
        encoding="utf-8",
    )
    (directory / "trades.csv").write_text(
        "side,start_day,end_day,bars,pnl\n1,0,1,4,40\n",
        encoding="utf-8",
    )
    if benchmark:
        nested = metrics.get("benchmarks") or {}
        for label, summary in nested.items():
            folder = directory / "benchmarks" / label
            folder.mkdir(parents=True, exist_ok=True)
            full = dict(summary)
            full.setdefault("initial_equity", metrics.get("initial_equity", 1_000_000))
            (folder / "metrics.json").write_text(json.dumps(full, indent=2) + "\n", encoding="utf-8")
            final = full.get("final_equity", full["initial_equity"])
            path = []
            count = len(series)
            for index in range(count):
                step = 0 if count == 1 else index / (count - 1)
                path.append(full["initial_equity"] + (final - full["initial_equity"]) * step)
            (folder / "equity_curve.csv").write_text(
                _equity(full["initial_equity"], path),
                encoding="utf-8",
            )
    return directory


ARENA_METRICS = {
    "profile": "arena",
    "symbol": "QQQ",
    "bar_minutes": 1,
    "initial_equity": 1_000_000,
    "final_equity": 1_040_000,
    "total_return": 0.04,
    "cagr": 0.12,
    "ann_vol": 0.15,
    "sharpe": 0.8,
    "sortino": 1.1,
    "max_drawdown": -0.08,
    "max_dd_duration_sessions": 3,
    "calmar": 1.5,
    "excess_total_return": 0.02,
    "n_fills": 2,
    "fees_breakdown": {"commission": 1.0, "sec": 0.2, "taf": 0.3},
    "period_returns": {
        "S01": 0.04,
        "S02": -0.012,
        "monthly": {"2024-01": 0.021},
        "yearly": {"2024": 0.04},
    },
    "fees_total": 1.5,
    "borrow_cost": 0.0,
    "sessions": 4,
    "years": 0.016,
    "cost_basis": {
        "share_scale": 1.0,
        "cost_schedule": "arena",
        "per_share_fees": "real_share_counts",
        "profile": "arena",
        "note": "Per-share fees use real share counts (share_scale=1.0).",
    },
    "benchmarks": {
        "buy_and_hold_1x": {
            "total_return": 0.02,
            "cagr": 0.06,
            "sharpe": 0.4,
            "max_drawdown": -0.05,
            "calmar": 1.2,
            "final_equity": 1_020_000,
            "fees_total": 0.4,
            "n_fills": 1,
        }
    },
}

DESK_METRICS = {
    "profile": "desk",
    "symbol": "SPY",
    "bar_minutes": 1,
    "initial_equity": 1_000_000,
    "final_equity": 612_000,
    "total_return": -0.388,
    "cagr": -0.41,
    "ann_vol": 0.22,
    "sharpe": -1.4,
    "sortino": -1.8,
    "max_drawdown": -0.72,
    "max_dd_duration_sessions": 18,
    "calmar": -0.57,
    "n_fills": 14,
    "fees_total": 840.5,
    "borrow_cost": 120.0,
    "financing_cost": 2100.0,
    "dividends_net": 340.0,
    "margin_calls_queued": 2,
    "margin_calls_filled": 2,
    "max_gross_leverage": 3.4,
    "avg_gross_leverage": 2.1,
    "worst_equity_over_hwm": 0.28,
    "disqualified": True,
    "liquidation": {
        "session": "2024-03-14",
        "equity": 280000.0,
        "high_water": 1000000.0,
        "fraction_of_hwm": 0.28,
    },
    "cost_basis": {
        "share_scale": 1.0,
        "cost_schedule": "desk",
        "per_share_fees": "real_share_counts",
        "profile": "desk",
        "note": "Per-share fees use real share counts (share_scale=1.0).",
    },
    "benchmarks": {
        "buy_and_hold_1x": {"total_return": 0.04, "final_equity": 1_040_000, "sharpe": 0.5, "n_fills": 1, "fees_total": 1.0},
        "long_2x": {"total_return": 0.05, "final_equity": 1_050_000, "financing_cost": 5848.14, "n_fills": 1, "fees_total": 1.2},
    },
}


def write_parallel_run(directory: Path, *, anonymized: bool = False) -> Path:
    """A Phase 3 output tree: jobs/, error.json, run.json, and an optional designer/."""
    directory.mkdir(parents=True, exist_ok=True)
    ok_metrics = {
        "profile": "arena",
        "symbol": "QQQ",
        "bar_minutes": 1,
        "initial_equity": 1_000_000,
        "final_equity": 1_100_000,
        "total_return": 0.041,
        "sharpe": 1.234,
        "calmar": float("inf"),
        "excess_total_return": 0.01,
        "anonymized": anonymized,
    }
    write_book(directory / "jobs" / "alpha" / "QQQ", ok_metrics, benchmark=False)
    failed = directory / "jobs" / "beta" / "QQQ"
    failed.mkdir(parents=True, exist_ok=True)
    (failed / "error.json").write_text(
        json.dumps(
            {
                "strategy_id": "beta",
                "source_id": "QQQ",
                "status": "error",
                "error": "RuntimeError: boom",
                "symbol": "QQQ",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (directory / "leaderboard.csv").write_text(
        "rank,strategy_id,source_id,symbol,total_return,sharpe,calmar,final_equity,excess_total_return\n"
        "1,alpha,QQQ,QQQ,0.041,1.234,inf,1100000,0.01\n"
        "2,beta,QQQ,QQQ,,,,,\n",
        encoding="utf-8",
    )
    if anonymized:
        clean = {key: value for key, value in ok_metrics.items() if key not in {"symbol", "anonymized"}}
        clean["profile"] = "arena"
        write_book(directory / "designer" / "alpha" / "QQQ", clean, benchmark=False)
        designer_failed = directory / "designer" / "beta" / "QQQ"
        designer_failed.mkdir(parents=True, exist_ok=True)
        (designer_failed / "error.json").write_text(
            json.dumps(
                {
                    "strategy_id": "beta",
                    "source_id": "QQQ",
                    "status": "error",
                    "error": "the strategy failed",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (directory / "designer" / "leaderboard.csv").write_text(
            "rank,strategy_id,source_id,total_return,sharpe,calmar,final_equity,excess_total_return\n"
            "1,alpha,QQQ,0.041,1.234,inf,1100000,\n"
            "2,beta,QQQ,,,,,\n",
            encoding="utf-8",
        )
    (directory / "run.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "parallel",
                "profile": "arena",
                "jobs": [
                    {"strategy_id": "alpha", "source_id": "QQQ", "symbol": "QQQ", "status": "ok", "anonymized": anonymized},
                    {"strategy_id": "beta", "source_id": "QQQ", "symbol": "QQQ", "status": "error", "anonymized": anonymized},
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return directory
