"""Loader, resampling, and the October 2026 sample."""

from __future__ import annotations

import gzip
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from backtester.data import (
    assert_no_silent_splits,
    discover_filled_files,
    frame_to_desk_bars,
    load_named,
    monthly_filled_filename,
    resample,
)

ROOT = Path(__file__).resolve().parents[1]
ET = timezone(timedelta(hours=-4))


def write_month(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = [
        "timestamp_et", "epoch_utc", "open", "high", "low", "close", "volume", "wap", "count", "filled", "session",
    ]
    lines = [",".join(header)]
    for row in rows:
        lines.append(",".join(str(row[name]) for name in header))
    with gzip.open(path, "wt", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")


def minute_row(day: datetime, minute: int, phase: str, price: float, *, filled: int = 0) -> dict:
    stamp = day.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=minute)
    stamp = stamp.replace(tzinfo=ET)
    return {
        "timestamp_et": stamp.isoformat(),
        "epoch_utc": int(stamp.timestamp()),
        "open": price,
        "high": price + 0.4,
        "low": price - 0.4,
        "close": price + 0.1,
        "volume": 10,
        "wap": price,
        "count": 3,
        "filled": filled,
        "session": phase,
    }


def test_sample_month_is_raw_and_complete():
    qqq = load_named(ROOT / "assets" / "sample" / "QQQ" / "filled", "QQQ")
    spy = load_named(ROOT / "assets" / "sample" / "SPY" / "filled", "SPY")
    assert qqq.inspection.share_factor == 1.0
    assert qqq.inspection.price_adjustment == "raw"
    assert qqq.inspection.n_rows == 21120
    assert qqq.inspection.n_sessions == 22
    assert spy.inspection.n_rows == 21120
    first = qqq.frame.iloc[0]
    assert first["timestamp_et"].startswith("2026-10-01T04:00:00")
    assert first["open"] == 743.99
    assert first["session"] == "pre"
    assert spy.frame.iloc[0]["open"] == 764.56
    assert set(qqq.frame["session"]) == {"pre", "rth", "post"}
    assert int((qqq.frame["session"] == "pre").sum()) == 7260
    assert int((qqq.frame["session"] == "rth").sum()) == 8580
    assert int((qqq.frame["session"] == "post").sum()) == 5280
    assert set(qqq.frame["short_session"]) == {0}
    assert qqq.frame["epoch_utc"].is_monotonic_increasing
    desk = frame_to_desk_bars(qqq.frame.iloc[:2])
    assert desk[0]["session"] == "2026-10-01"
    assert desk[0]["phase"] == "pre"
    assert desk[0]["count"] == int(qqq.frame.iloc[0]["count"])
    assert desk[0]["new_session"] is True
    assert desk[1]["new_session"] is False


def test_real_sample_resample_stays_inside_the_session():
    loaded = load_named(ROOT / "assets" / "sample" / "QQQ" / "filled", "QQQ", bar_minutes=5)
    frame = loaded.frame
    assert len(frame) == 4224
    assert int(frame.iloc[0]["minute"]) == 240
    assert frame.iloc[0]["session"] == "pre"
    first_rth = frame.loc[frame["session"] == "rth"].iloc[0]
    assert int(first_rth["minute"]) == 570
    last_pre = frame.loc[frame["session"] == "pre"].iloc[-1]
    assert int(frame.loc[frame["session"] == "pre", "minute"].max()) == 565
    assert int(last_pre["minute"]) <= 565
    assert not ((frame["session"] == "rth") & (frame["minute"] == 565)).any()
    assert set(frame["short_session"]) == {0}


def test_short_session_is_measured_before_resample(tmp_path):
    day = datetime(2026, 10, 1)
    early = []
    for minute, phase in ((240, "pre"), (570, "rth"), (779, "rth"), (960, "post")):
        early.append(minute_row(day, minute, phase, 10))
    full = []
    nxt = datetime(2026, 10, 2)
    for minute, phase in ((240, "pre"), (570, "rth"), (959, "rth"), (960, "post")):
        full.append(minute_row(nxt, minute, phase, 11))
    path = tmp_path / "QQQ_1min_filled_2026-10.csv.gz"
    write_month(path, early + full)
    loaded = load_named(path, "QQQ", bar_minutes=30)
    flags = loaded.frame.groupby("date")["short_session"].first()
    assert int(flags["2026-10-01"]) == 1
    assert int(flags["2026-10-02"]) == 0
    # The early day's last RTH minute is 779. A 30-minute bucket must not relabel the day.
    assert int(loaded.frame.loc[loaded.frame["date"] == "2026-10-01", "short_session"].max()) == 1


def test_resample_anchor_partial_tail_and_session_walls():
    rows = []
    for offset, minute in enumerate(range(571, 578)):
        rows.append(
            {
                "timestamp_et": f"2026-10-01T{minute // 60:02d}:{minute % 60:02d}:00-04:00",
                "epoch_utc": 1_700_000_000 + minute * 60,
                "date": "2026-10-01",
                "day": 0,
                "weekday": 3,
                "minute": minute,
                "time": f"{minute // 60:02d}:{minute % 60:02d}",
                "session": "rth",
                "open": 10.0 + offset,
                "high": 11.0 + offset,
                "low": 9.0 + offset,
                "close": 10.5 + offset,
                "volume": 10.0 if offset == 0 else 30.0,
                "wap": 10.0 if offset == 0 else 20.0,
                "count": 1,
                "filled": 1 if offset else 0,
                "short_session": 0,
                "symbol": "QQQ",
            }
        )
    # A post segment that a midnight grid would have glued onto the last RTH bucket.
    for minute in range(960, 964):
        rows.append(
            {
                "timestamp_et": f"2026-10-01T{minute // 60:02d}:{minute % 60:02d}:00-04:00",
                "epoch_utc": 1_700_000_000 + minute * 60,
                "date": "2026-10-01",
                "day": 0,
                "weekday": 3,
                "minute": minute,
                "time": f"{minute // 60:02d}:{minute % 60:02d}",
                "session": "post",
                "open": 5,
                "high": 5,
                "low": 5,
                "close": 5,
                "volume": 1,
                "wap": 5,
                "count": 1,
                "filled": 1,
                "short_session": 0,
                "symbol": "QQQ",
            }
        )
    frame = pd.DataFrame(rows)
    out = resample(frame, 5)
    rth = out.loc[out["session"] == "rth"]
    assert list(rth["minute"]) == [571, 576]
    assert 570 not in set(rth["minute"])
    assert float(rth.iloc[0]["open"]) == 10.0
    assert float(rth.iloc[0]["close"]) == 10.5 + 4
    assert int(rth.iloc[0]["filled"]) == 0
    assert int(rth.iloc[1]["filled"]) == 1
    # First bucket volumes 10 then 30*4, wap 10 then 20.
    assert float(rth.iloc[0]["wap"]) == (10 * 10 + 20 * 30 * 4) / (10 + 120)
    assert list(out.loc[out["session"] == "post", "minute"]) == [960]
    copied = resample(frame, 1)
    assert len(copied) == len(frame)
    assert list(copied["open"]) == list(frame["open"])


def test_resample_restarts_when_the_minute_goes_backwards():
    rows = []
    for minute in (571, 572, 571, 572):
        rows.append(
            {
                "timestamp_et": f"2026-10-01T{minute // 60:02d}:{minute % 60:02d}:00-04:00",
                "epoch_utc": 1,
                "date": "2026-10-01",
                "day": 0,
                "weekday": 3,
                "minute": minute,
                "time": f"{minute // 60:02d}:{minute % 60:02d}",
                "session": "rth",
                "open": 1, "high": 1, "low": 1, "close": 1,
                "volume": 1, "wap": 1, "count": 1, "filled": 0, "short_session": 0, "symbol": "QQQ",
            }
        )
    out = resample(pd.DataFrame(rows), 5)
    assert list(out["minute"]) == [571, 571]


def test_months_sort_and_bad_rows_raise(tmp_path):
    later = tmp_path / "SPY_1min_filled_2026-11.csv.gz"
    earlier = tmp_path / "SPY_1min_filled_2026-10.csv.gz"
    write_month(later, [minute_row(datetime(2026, 11, 2), 570, "rth", 3)])
    write_month(earlier, [minute_row(datetime(2026, 10, 1), 570, "rth", 2), minute_row(datetime(2026, 10, 1), 571, "rth", 2)])
    found = discover_filled_files(tmp_path, "SPY")
    assert [path.name for path in found] == [earlier.name, later.name]
    loaded = load_named(tmp_path, "SPY")
    assert list(loaded.frame["date"].unique()) == ["2026-10-01", "2026-11-02"]
    assert list(loaded.frame["day"].unique()) == [0, 1]
    with pytest.raises(ValueError, match="Split"):
        load_named(tmp_path, "SPY", splits=((datetime(2026, 8, 1).date(), 2.0),))
    assert_no_silent_splits(())
    broken = [minute_row(datetime(2026, 10, 1), 570, "rth", 2)]
    broken[0]["session"] = "overnight"
    path = tmp_path / "odd" / "QQQ_1min_filled_2026-10.csv.gz"
    write_month(path, broken)
    with pytest.raises(ValueError, match="session labels"):
        load_named(path, "QQQ")


def test_count_survives_the_desk_conversion():
    frame = pd.DataFrame(
        [{
            "date": "2026-10-01",
            "session": "rth",
            "minute": 570,
            "time": "09:30",
            "open": 1.0,
            "high": 1.0,
            "low": 1.0,
            "close": 1.0,
            "volume": 1.0,
            "wap": 1.0,
            "count": 7,
            "filled": 0,
        }]
    )
    assert frame_to_desk_bars(frame)[0]["count"] == 7


def test_filename_rejects_a_path_separator():
    assert monthly_filled_filename("BRK.B", 2024, 1) == "BRK.B_1min_filled_2024-01.csv.gz"
    with pytest.raises(ValueError):
        monthly_filled_filename("QQQ/filled", 2024, 1)
