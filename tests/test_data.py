"""Loader, resampling, and the October 2026 sample."""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from backtester.data import (
    REQUIRED_COLUMNS,
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
    assert list(rth["partial"].astype(int)) == [0, 1]
    assert list(out.loc[out["session"] == "post", "partial"].astype(int)) == [1]
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
    assert set(copied["partial"].astype(int)) == {0}


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
    assert not any(str(note).startswith("Missing monthly file") for note in loaded.inspection.notes)
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


def test_missing_month_is_named_and_a_gap_inside_a_file_is_kept(tmp_path, capsys):
    from backtester.__main__ import main

    # 2025-11-03 and 2025-11-06, with 2025-11-05 absent inside the file.
    november = [
        minute_row(datetime(2025, 11, 3), 570, "rth", 10),
        minute_row(datetime(2025, 11, 3), 571, "rth", 10),
        minute_row(datetime(2025, 11, 6), 570, "rth", 11),
        minute_row(datetime(2025, 11, 6), 571, "rth", 11),
    ]
    february = [
        minute_row(datetime(2026, 2, 2), 570, "rth", 12),
        minute_row(datetime(2026, 2, 2), 571, "rth", 12),
    ]
    write_month(tmp_path / "QQQ_1min_filled_2025-11.csv.gz", november)
    write_month(tmp_path / "QQQ_1min_filled_2026-02.csv.gz", february)
    loaded = load_named(tmp_path, "QQQ")
    assert list(loaded.frame["date"].unique()) == ["2025-11-03", "2025-11-06", "2026-02-02"]
    notes = [note for note in loaded.inspection.notes if str(note).startswith("Missing monthly file")]
    assert len(notes) == 1
    assert "2025-12" in notes[0]
    assert "2026-01" in notes[0]
    assert "2025-11-05" not in notes[0]
    assert "between 2025-11 and 2026-02" in notes[0]

    out = tmp_path / "inspect.json"
    assert main(["ingest", "--ticker", "QQQ", "--src", str(tmp_path), "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "note: Missing monthly file" in printed
    assert "2025-12" in printed
    assert "2026-01" in printed
    hold = Path(__file__).resolve().parents[1] / "examples" / "desk" / "buy_and_hold.py"
    assert main(
        [
            "run", "--profile", "desk", "--ticker", "QQQ", "--strategy", str(hold),
            "--src", str(tmp_path), "--out", str(tmp_path / "run"),
        ]
    ) == 0
    run_out = capsys.readouterr().out
    assert "note: Missing monthly file" in run_out
    assert "2025-12" in run_out

    assert main(
        [
            "walkforward", "--profile", "desk", "--ticker", "QQQ", "--strategy", str(hold),
            "--src", str(tmp_path), "--out", str(tmp_path / "walk"),
            "--design-end-date", "2025-11-06",
        ]
    ) == 0
    walk_out = capsys.readouterr().out
    assert "note: Missing monthly file" in walk_out
    assert "2025-12" in walk_out
    assert "2026-01" in walk_out
    summary = json.loads((tmp_path / "walk" / "walkforward.json").read_text(encoding="utf-8"))
    written = [note for note in summary["inspection_notes"] if str(note).startswith("Missing monthly file")]
    assert len(written) == 1
    assert "2025-12" in written[0]
    assert "2026-01" in written[0]
    assert "2025-11-05" not in written[0]


def test_filename_rejects_a_path_separator():
    assert monthly_filled_filename("BRK.B", 2024, 1) == "BRK.B_1min_filled_2024-01.csv.gz"
    with pytest.raises(ValueError):
        monthly_filled_filename("QQQ/filled", 2024, 1)


def test_partial_bucket_is_not_padded_and_does_not_take_the_next_session(tmp_path):
    """A short session keeps its tail. The next day's price stays in the next day."""
    day = datetime(2026, 6, 1)
    nxt = datetime(2026, 6, 2)
    rows = []
    for offset, minute in enumerate(range(480, 487)):
        rows.append(minute_row(day, minute, "rth", 10.0 + offset))
    rows.append(minute_row(nxt, 480, "rth", 999.0))
    path = tmp_path / "NIK_1min_filled_2026-06.csv.gz"
    write_month(path, rows)
    loaded = load_named(path, "NIK", bar_minutes=5)
    first = loaded.frame.loc[loaded.frame["date"] == "2026-06-01"]
    second = loaded.frame.loc[loaded.frame["date"] == "2026-06-02"]
    assert list(first["minute"].astype(int)) == [480, 485]
    assert list(first["partial"].astype(int)) == [0, 1]
    assert float(first.iloc[0]["open"]) == 10.0
    assert float(first.iloc[0]["high"]) < 20.0
    assert float(first.iloc[1]["high"]) < 20.0
    assert float(second.iloc[0]["open"]) == 999.0
    assert int(second.iloc[0]["partial"]) == 1
    assert float(first["high"].max()) != 999.0
    rth = loaded.inspection.session_segments[0]
    assert rth.label == "rth"
    assert rth.start_minute == 480
    assert (rth.start_minute, rth.end_minute) != (4 * 60, 19 * 60 + 59)


def test_gapped_bucket_is_partial_even_when_the_span_looks_full():
    rows = []
    for minute in (571, 572, 573, 574, 576):
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
                "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0,
                "volume": 10.0, "wap": 10.0, "count": 1, "filled": 0,
                "short_session": 0, "symbol": "QQQ",
            }
        )
    out = resample(pd.DataFrame(rows), 5)
    assert list(out["minute"].astype(int)) == [571, 576]
    assert list(out["partial"].astype(int)) == [1, 1]
    assert int(out.iloc[0]["count"]) == 4


def test_hours_are_inferred_and_not_a_us_template(tmp_path):
    day = datetime(2026, 3, 2)
    rows = [minute_row(day, minute, "rth", 10.0) for minute in (8 * 60, 14 * 60)]
    path = tmp_path / "NIK_1min_filled_2026-03.csv.gz"
    write_month(path, rows)
    loaded = load_named(path, "NIK")
    segment = loaded.inspection.session_segments[0]
    assert (segment.start_minute, segment.end_minute) == (8 * 60, 14 * 60)
    assert segment.as_dict()["start_clock"] == "08:00"
    assert segment.as_dict()["end_clock"] == "14:00"
    assert segment.gapped_sessions == 1
    assert set(loaded.frame["short_session"].astype(int)) == {1}
    assert any("15:59" in note for note in loaded.inspection.notes)
    assert loaded.inspection.as_dict()["hours_assumed"] is False
    assert "04:00" not in segment.as_dict()["start_clock"]


def test_ambiguous_series_fail_loudly(tmp_path):
    day = datetime(2026, 4, 1)
    foreign = [minute_row(day, 480, "rth", 10.0)]
    foreign[0]["timestamp_et"] = "2026-04-01T08:00:00+09:00"
    foreign[0]["epoch_utc"] = int(datetime.fromisoformat(foreign[0]["timestamp_et"]).timestamp())
    path = tmp_path / "NIK_1min_filled_2026-04.csv.gz"
    write_month(path, foreign)
    with pytest.raises(ValueError, match="offset"):
        load_named(path, "NIK")

    pair = [
        minute_row(day, 480, "rth", 10.0),
        minute_row(day, 481, "rth", 11.0),
    ]
    pair[1]["timestamp_et"] = "2026-04-01T08:01:00-06:00"
    pair[1]["epoch_utc"] = int(datetime.fromisoformat(pair[1]["timestamp_et"]).timestamp())
    write_month(path, pair)
    with pytest.raises(ValueError, match="contradict the column name"):
        load_named(path, "NIK")

    broken = [
        minute_row(day, 480, "pre", 10.0),
        minute_row(day, 481, "rth", 10.0),
        minute_row(day, 482, "pre", 10.0),
    ]
    write_month(path, broken)
    with pytest.raises(ValueError, match="discontinuous"):
        load_named(path, "NIK")

    same_minute = [
        minute_row(day, 480, "rth", 10.0),
        minute_row(day, 480, "post", 11.0),
    ]
    same_minute[1]["timestamp_et"] = "2026-04-01T08:00:00-05:00"
    same_minute[1]["epoch_utc"] = int(datetime.fromisoformat(same_minute[1]["timestamp_et"]).timestamp())
    write_month(path, same_minute)
    with pytest.raises(ValueError, match="Which session owns that minute"):
        load_named(path, "NIK")

    stale_bad = [
        minute_row(day, 480, "rth", 10.0),
        minute_row(day, 481, "rth", 10.0, filled=1),
    ]
    write_month(path, stale_bad)
    with pytest.raises(ValueError, match="Fill policy|filled"):
        load_named(path, "NIK")

    flat = minute_row(day, 480, "rth", 10.0)
    stale = minute_row(day, 481, "rth", 10.1, filled=1)
    stale["open"] = stale["high"] = stale["low"] = stale["close"] = flat["close"]
    stale["volume"] = 0
    stale["wap"] = flat["close"]
    write_month(path, [flat, stale])
    loaded = load_named(path, "NIK")
    assert loaded.inspection.fill_policy == "stale_forward_fill"
    assert loaded.inspection.stale_when_filled is True

    tied_a = [minute_row(datetime(2026, 4, 1), minute, "rth", 10.0) for minute in (480, 481)]
    tied_b = [minute_row(datetime(2026, 4, 2), minute, "rth", 12.0) for minute in (500, 501, 502)]
    write_month(path, tied_a + tied_b)
    tied = load_named(path, "NIK")
    assert any("no unique modal window" in note for note in tied.inspection.notes)
    assert tied.inspection.session_segments[0].end_minute == 502
    assert tied.inspection.session_segments[0].variants

    spring = [minute_row(datetime(2026, 3, 2), minute, "rth", 10.0) for minute in (480, 481)]
    fall = [minute_row(datetime(2026, 11, 2), minute, "rth", 12.0) for minute in (480, 481)]
    for row in fall:
        stamp = datetime.fromisoformat(row["timestamp_et"]).replace(tzinfo=timezone(timedelta(hours=-5)))
        row["timestamp_et"] = stamp.isoformat()
        row["epoch_utc"] = int(stamp.timestamp())
    write_month(path, spring + fall)
    dst = load_named(path, "NIK")
    assert dst.inspection.timezone_offsets == ("-04:00", "-05:00")
    assert dst.inspection.timezone_dst_pair is True
    assert dst.inspection.session_segments[0].start_minute == 480

    one_price = [minute_row(day, 480, "rth", 10.0)]
    one_price[0]["open"] = one_price[0]["high"] = one_price[0]["low"] = one_price[0]["close"] = 10.0
    one_price[0]["wap"] = 10.0
    write_month(path, one_price)
    with pytest.raises(ValueError, match="tick"):
        load_named(path, "NIK")


def _write_columns(path: Path, rows: list[dict], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [",".join(columns)]
    for row in rows:
        lines.append(",".join(str(row[name]) for name in columns))
    with gzip.open(path, "wt", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")


def test_extra_columns_are_named_and_rejected(tmp_path):
    row = minute_row(datetime(2024, 6, 3), 9 * 60 + 30, "rth", 10.0)
    row["vendor"] = "tape"
    path = tmp_path / "NIK_1min_filled_2024-06.csv.gz"
    _write_columns(path, [row], list(REQUIRED_COLUMNS) + ["vendor"])
    with pytest.raises(ValueError, match="extra columns: vendor"):
        load_named(path.parent, "NIK")


def test_columns_are_read_by_name_when_the_file_order_changes(tmp_path):
    """A position-based read would put the close into another field."""
    row = minute_row(datetime(2024, 6, 3), 9 * 60 + 30, "rth", 10.0)
    row["open"] = 10
    row["high"] = 12
    row["low"] = 9
    row["close"] = 11
    order = [
        "close", "session", "filled", "count", "wap", "volume",
        "low", "high", "open", "epoch_utc", "timestamp_et",
    ]
    assert set(order) == set(REQUIRED_COLUMNS)
    assert order != list(REQUIRED_COLUMNS)
    path = tmp_path / "NIK_1min_filled_2024-06.csv.gz"
    _write_columns(path, [row], order)
    loaded = load_named(path.parent, "NIK")
    first = loaded.frame.iloc[0]
    assert first["open"] == 10
    assert first["close"] == 11
    assert first["high"] == 12
    assert first["low"] == 9
    assert first["session"] == "rth"


def test_duplicate_and_out_of_order_timestamps_are_rejected(tmp_path):
    day = datetime(2024, 6, 3)
    path = tmp_path / "NIK_1min_filled_2024-06.csv.gz"
    write_month(path, [minute_row(day, 570, "rth", 10.0), minute_row(day, 570, "rth", 11.0)])
    with pytest.raises(ValueError, match="strictly increasing"):
        load_named(path.parent, "NIK")
    write_month(path, [minute_row(day, 571, "rth", 10.0), minute_row(day, 570, "rth", 11.0)])
    with pytest.raises(ValueError, match="strictly increasing"):
        load_named(path.parent, "NIK")
