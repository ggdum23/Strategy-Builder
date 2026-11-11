"""Write metrics.json, the equity curve, and the fill and trade files."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd


def percent_up_days(equity, initial):
    if not equity:
        return None
    previous = initial
    up = 0
    for value in equity:
        if value / previous - 1.0 > 0.0:
            up += 1
        previous = value
    return up / len(equity)


def json_ready(value):
    """Strict JSON: non-finite floats become null."""
    if isinstance(value, dict):
        return {str(key): json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_ready(item) for item in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return None
    if hasattr(value, "item"):
        return json_ready(value.item())
    return value


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(json_ready(payload), indent=2, allow_nan=False)
    path.write_text(text + "\n", encoding="utf-8")


def _dates_by_day(frame) -> dict:
    out = {}
    if frame is None:
        return out
    for day, date in zip(frame["day"].tolist(), frame["date"].tolist()):
        out.setdefault(int(day), str(date))
    return out


def write_arena_curves(directory: Path, result: dict, frame) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    dates = _dates_by_day(frame)
    equity_rows = []
    for day, equity, position, shares, close in result["daily"]:
        equity_rows.append(
            {
                "day": int(day),
                "date": dates.get(int(day), ""),
                "equity": equity,
                "position": position,
                "shares": shares,
                "close": close,
            }
        )
    pd.DataFrame(equity_rows).to_csv(directory / "equity_curve.csv", index=False)
    fills = []
    for day, minute, session, delta, open_, fill_price, fees, shares_after in result["fills"]:
        fills.append(
            {
                "day": int(day),
                "date": dates.get(int(day), ""),
                "minute": minute,
                "session": session,
                "shares_delta": delta,
                "open": open_,
                "fill_price": fill_price,
                "fees": fees,
                "shares_after": shares_after,
            }
        )
    pd.DataFrame(fills).to_csv(directory / "fills.csv", index=False)
    pd.DataFrame(result["episodes"]).to_csv(directory / "trades.csv", index=False)


def write_desk_curves(directory: Path, result: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(result["daily"]).to_csv(directory / "equity_curve.csv", index=False)
    fill_columns = [
        "session",
        "time",
        "phase",
        "minute",
        "shares_delta",
        "price",
        "fill_price",
        "fees",
        "commission",
        "exchange",
        "clearing",
        "pass_through",
        "cat",
        "sec",
        "taf",
        "shares_after",
        "kind",
        "tier_month",
    ]
    fills = [{key: row.get(key) for key in fill_columns} for row in result["fills"]]
    pd.DataFrame(fills).to_csv(directory / "fills.csv", index=False)
    pd.DataFrame(result["episodes"]).to_csv(directory / "trades.csv", index=False)


def enrich_arena_metrics(result: dict, metrics: dict) -> dict:
    out = dict(metrics)
    equity = [row[1] for row in result["daily"]]
    out["percent_up_days"] = percent_up_days(equity, result["initial_equity"])
    return out


def write_book(directory: Path, profile: str, result: dict, metrics: dict, frame) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    if profile == "arena":
        write_arena_curves(directory, result, frame)
    elif profile == "desk":
        write_desk_curves(directory, result)
    else:
        raise ValueError(f"unknown profile {profile}")
    write_json(directory / "metrics.json", metrics)
