"""Registry schema, inferred hours, and the ingest/list/show commands."""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backtester.__main__ import main
from backtester.data import load_named
from backtester.registry import (
    entry_from_inspection,
    format_list,
    format_show,
    load_registry,
    save_registry,
    upsert_series,
    validate_registry,
)

ET = timezone(timedelta(hours=-4))

ROOT = Path(__file__).resolve().parents[1]


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = [
        "timestamp_et", "epoch_utc", "open", "high", "low", "close", "volume", "wap", "count", "filled", "session",
    ]
    lines = [",".join(header)]
    for row in rows:
        lines.append(",".join(str(row[name]) for name in header))
    with gzip.open(path, "wt", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")


def _row(day: datetime, minute: int, phase: str, price: float) -> dict:
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
        "filled": 0,
        "session": phase,
    }


def _entry(tmp_path, symbol="NIK", profile="desk", **kwargs):
    day = datetime(2026, 5, 4)
    rows = [_row(day, minute, "rth", 10.0) for minute in (8 * 60, 8 * 60 + 1, 14 * 60)]
    path = tmp_path / f"{symbol}_1min_filled_2026-05.csv.gz"
    _write(path, rows)
    loaded = load_named(path, symbol)
    return entry_from_inspection(loaded.inspection, source_root=str(tmp_path), profile=profile, **kwargs), loaded


def test_schema_rejects_guesses(tmp_path):
    entry, _loaded = _entry(tmp_path)
    document = {"schema_version": 1, "series": [entry]}
    validate_registry(document)
    broken = json.loads(json.dumps(document))
    broken["schema_version"] = 2
    with pytest.raises(ValueError, match="schema"):
        validate_registry(broken)
    extra = json.loads(json.dumps(document))
    extra["series"][0]["nickname"] = "guess"
    with pytest.raises(ValueError, match="unknown field"):
        validate_registry(extra)
    scaled = json.loads(json.dumps(document))
    scaled["series"][0]["share_factor"] = 0.9
    with pytest.raises(ValueError, match="share_factor"):
        validate_registry(scaled)
    doubled = json.loads(json.dumps(document))
    doubled["series"].append(json.loads(json.dumps(entry)))
    with pytest.raises(ValueError, match="twice"):
        validate_registry(doubled)
    assumed = json.loads(json.dumps(document))
    assumed["series"][0]["hours_assumed"] = True
    with pytest.raises(ValueError, match="hours_assumed"):
        validate_registry(assumed)


def test_inferred_hours_round_trip_and_a_second_ticker_is_an_entry(tmp_path):
    nik, loaded = _entry(tmp_path, symbol="NIK", profile="desk")
    spy_rows = [_row(datetime(2026, 5, 4), minute, "rth", 20.0) for minute in (9 * 60, 9 * 60 + 1)]
    spy_path = tmp_path / "spy" / "SPY_1min_filled_2026-05.csv.gz"
    _write(spy_path, spy_rows)
    spy_loaded = load_named(spy_path.parent, "SPY")
    spy = entry_from_inspection(spy_loaded.inspection, source_root=str(spy_path.parent), profile="arena")
    registry = tmp_path / "registry.json"
    save_registry(registry, upsert_series(upsert_series({"schema_version": 1, "series": []}, nik), spy))
    document = load_registry(registry)
    assert [item["symbol"] for item in document["series"]] == ["NIK", "SPY"]
    assert document["series"][0]["session_segments"][0]["start_clock"] == "08:00"
    assert document["series"][0]["session_segments"][0]["end_clock"] == "14:00"
    assert document["series"][0]["hours_assumed"] is False
    assert document["series"][0]["cost_schedule"] == "desk"
    assert document["series"][1]["cost_schedule"] == "arena"
    assert document["series"][0]["cost_schedule"] != document["series"][1]["cost_schedule"]
    text = format_list(document)
    assert "NIK" in text and "SPY" in text
    show = format_show(document["series"][0])
    assert "08:00-14:00" in show
    assert "profile: desk" in show
    assert loaded.inspection.share_factor == 1.0


def test_splits_and_total_return_are_recorded_and_not_runnable(tmp_path):
    day = datetime(2026, 5, 4)
    rows = [_row(day, minute, "core", 10.0) for minute in (8 * 60, 8 * 60 + 1)]
    path = tmp_path / "NIK_1min_filled_2026-05.csv.gz"
    _write(path, rows)
    with pytest.raises(ValueError, match="session labels"):
        load_named(path, "NIK")
    loaded = load_named(path, "NIK", phase_map={"core": "rth"})
    assert list(loaded.frame["session"]) == ["rth", "rth"]
    assert loaded.inspection.original_session_labels == ("core",)
    raw_open = float(loaded.frame.iloc[0]["open"])
    entry = entry_from_inspection(
        loaded.inspection,
        source_root=str(tmp_path),
        profile="arena",
        splits=({"date": "2020-08-31", "ratio": 4.0},),
    )
    assert entry["runnable"] is False
    assert entry["splits"] == [{"date": "2020-08-31", "ratio": 4.0}]
    again = load_named(path, "NIK", phase_map={"core": "rth"})
    assert float(again.frame.iloc[0]["open"]) == raw_open
    total = load_named(path, "NIK", phase_map={"core": "rth"}, price_kind="total_return")
    total_entry = entry_from_inspection(total.inspection, source_root=str(tmp_path), profile="arena")
    assert total_entry["price_adjustment"] == "total_return"
    assert total_entry["runnable"] is False
    assert float(total.frame.iloc[0]["open"]) == raw_open


def test_ingest_and_registry_commands(tmp_path, capsys):
    src = ROOT / "assets" / "sample" / "QQQ" / "filled"
    spy = ROOT / "assets" / "sample" / "SPY" / "filled"
    registry = tmp_path / "registry.json"
    assert main(
        [
            "ingest", "--ticker", "QQQ", "--src", str(src), "--registry", str(registry),
            "--profile", "arena", "--price-kind", "raw",
        ]
    ) == 0
    assert main(
        [
            "ingest", "--ticker", "SPY", "--src", str(spy), "--registry", str(registry),
            "--profile", "desk", "--price-kind", "raw", "--borrow-rate", "0.01",
        ]
    ) == 0
    listed = capsys.readouterr()
    assert main(["registry", "list", "--registry", str(registry)]) == 0
    text = capsys.readouterr().out
    assert "QQQ" in text and "SPY" in text
    assert "arena" in text and "desk" in text
    assert main(["registry", "show", "--registry", str(registry), "--ticker", "QQQ"]) == 0
    show = capsys.readouterr().out
    assert "04:00-09:29" in show
    assert "09:30-15:59" in show
    assert "16:00-19:59" in show
    assert "profile: arena" in show
    assert "share_factor: 1.0" in show
    document = load_registry(registry)
    by_symbol = {item["symbol"]: item for item in document["series"]}
    assert by_symbol["QQQ"]["borrow_rate_source"] == "profile_default"
    assert by_symbol["QQQ"]["borrow_rate_annual"] == 0.003
    assert by_symbol["SPY"]["borrow_rate_source"] == "explicit"
    assert by_symbol["SPY"]["borrow_rate_annual"] == 0.01
    assert by_symbol["QQQ"]["n_sessions"] == 22
    assert by_symbol["QQQ"]["native_tick"] == 0.01
    refused = main(["ingest", "--ticker", "QQQ", "--src", str(src), "--registry", str(registry)])
    assert refused == 2
    err = capsys.readouterr().err
    assert "profile" in err
    assert listed.out  # the first ingest told the operator what it inferred
