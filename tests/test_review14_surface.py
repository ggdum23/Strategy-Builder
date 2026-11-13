"""Fourteenth review. Designer strategy ids use one allow-list grammar."""

from __future__ import annotations

import json

import pytest

from backtester.calendar_text import contains_calendar_date, strip_calendar_dates
from backtester.designer_ids import strategy_id_problem
from backtester.gui.audience import export_allowed, export_block_reason, scan_publication, scrub_text
from backtester.report import _designer_value_error, assert_designer_clean, designer_component_problem, read_leaderboard
from test_parallel import FLAT, _anon_pack, _strategy
from windows_open import windows_jails

# Spellings from reviews 13 and 14, plus two-digit years, epochs, and weekdays.
DATES = (
    "04-Jan-2024",
    "2024-1-4",
    "January 4, 2024",
    "01.04.2024",
    "today 2024",
    "2024-01",
    "January 2024",
    "04.Jan.2024",
    "Jan.4, 2024",
    "1.4.2024",
    "holiday 2024",
    "day 2024",
    "Day 2024",
    "DAY 2024",
    "Jan 4th 2024",
    "4 January 2024",
    "Q1 2024",
    "wk1-2024",
    "04-JAN-2024",
    "4\u2011Jan\u20112024",
    "20240104",
    "note_20240104",
    "20240104_note",
    "01042024",
    "04012024",
    "\uff12\uff10\uff12\uff14\uff10\uff11\uff10\uff14",
    "\uff12\uff10\uff12\uff14-\uff10\uff11-\uff10\uff14",
    "4-Jan-24",
    "Jan-24",
    "01-04-24",
    "01\u201504\u201524",
    "01\u200b04\u200b24",
    "01 04 24",
    "01\u00a004\u00a024",
    "01\u200304\u200324",
    "01\u300004\u300024",
    "01\u00ad04\u00ad24",
    "01\u201404\u201424",
    "4\u2015Jan\u201524",
    "Jan\u201524",
    "4 Jan 24",
    "Jan 4, 24",
    "alpha_2024-01-04_v2",
    "FY2024",
    "fy2024",
    "2024Q1",
    "h1_2024",
    "1_4_24",
    "Monday",
    "Mon",
    "Friday",
    "Fri",
    "today",
    "holiday",
    "yesterday",
    "tomorrow",
    "eod",
    "1704067200",
    "note_1704067200",
)
PUBLISH = (
    "long_2x",
    "ma_200",
    "ma_20_50",
    "rsi14",
    "v2",
    "opening_drive",
    "mean_rev_3",
    "breakout_v12",
    "RANDOM1",
    "q1",
    "h1",
    "h2",
    "fy",
    "strategy_q1",
)
_SCALE = 1.1


def _date_hits(text: str) -> tuple[bool, bool, bool, bool, bool]:
    """The five gates, as a date verdict. A price or an unsafe name is not a date."""
    component = designer_component_problem(text, symbols=(), scale=_SCALE) == "a calendar date"
    value = _designer_value_error("caption", text, "") == "a calendar date"
    scanned = scan_publication(text, scale=_SCALE) == "a date"
    scrubbed = scrub_text(text, (), scale=_SCALE) != text
    exported = export_block_reason(text, symbols=(), scale=_SCALE) == "a date"
    return (component, value, scanned, scrubbed, exported)


@pytest.mark.parametrize("text", DATES)
def test_every_gate_rejects_the_spelling(text):
    assert _date_hits(text) == (True, True, True, True, True), text
    assert contains_calendar_date(text)
    assert strip_calendar_dates(text) != text
    assert scrub_text(text, ()) == strip_calendar_dates(text)
    assert strategy_id_problem(text) == "a calendar date"


@pytest.mark.parametrize("text", PUBLISH)
def test_publishable_ids_pass_every_gate(text):
    assert _date_hits(text) == (False, False, False, False, False), text
    assert strategy_id_problem(text) is None
    assert designer_component_problem(text, symbols=(), scale=_SCALE) is None
    assert scrub_text(text, (), scale=_SCALE) == text


def test_other_dashes_spaces_and_format_characters_are_dates():
    """A separator is any non-alphanumeric, after format characters become a break."""
    samples = (
        "01\u201504\u201524",  # horizontal bar, not folded to ASCII hyphen
        "01\u200b04\u200b24",  # zero-width space
        "01\u00ad04\u00ad24",  # soft hyphen
        "01\u00a004\u00a024",  # no-break space
        "01\u200304\u200324",  # em space
        "01\u200904\u200924",  # thin space
        "01\u300004\u300024",  # ideographic space
        "01\u201404\u201424",  # em dash
        "01\u221204\u221224",  # minus
        "4\u2015Jan\u201524",
        "Jan\u201524",
        "01 04 24",
    )
    for text in samples:
        assert _date_hits(text) == (True, True, True, True, True), text
        assert contains_calendar_date(text)
        assert strategy_id_problem(text) == "a calendar date"


def test_clocks_and_decimals_stay_out_of_the_separator_scan():
    for text in (
        "09:25:00",
        "09:25",
        "07:30",
        "2024.50",
        "0.2024",
        "count 12 and 20",
        "long_2x",
        "ma_20_50",
        "ratio 2.5",
        "2.5 2024.50",
        "2024.50 0.2024",
        "2024.50 0",
    ):
        assert not contains_calendar_date(text), text
        assert scrub_text(text, ()) == text
        assert strip_calendar_dates(text) == text
    kept = "long_2x 2.5 2024.50 0.2024 day 2024 count 12"
    assert strip_calendar_dates(kept) == "long_2x 2.5 2024.50 0.2024 day  count 12"


def test_decimals_stay_numbers_and_are_not_ids():
    for text in ("2024.50", "0.2024"):
        assert _date_hits(text) == (False, False, False, False, False)
        assert strategy_id_problem(text) == "an unsafe name"
        assert designer_component_problem(text, symbols=(), scale=_SCALE) == "an unsafe name"


def test_session_labels_are_not_free_text_dates():
    assert contains_calendar_date("S00001") is False
    assert scrub_text("S00001", ()) == "S00001"
    assert scan_publication("S00001", scale=_SCALE) is None
    assert strategy_id_problem("S00001") == "a calendar date"
    assert strategy_id_problem("S01") is None


def test_slash_date_stays_an_unsafe_name():
    assert designer_component_problem("2024/01/04", symbols=(), scale=_SCALE) == "an unsafe name"
    assert contains_calendar_date("2024/01/04")
    assert scan_publication("2024/01/04", scale=_SCALE) == "a date"
    assert export_block_reason("2024/01/04", symbols=(), scale=_SCALE) == "a date"


def test_listed_numbers_are_masked_before_the_date_scan():
    equity = json.dumps({"initial_equity": 20240104, "sharpe": 1.2})
    assert contains_calendar_date(equity)
    assert scan_publication(equity, scale=_SCALE) is None
    assert export_block_reason(equity, symbols=(), scale=_SCALE) is None
    assert scan_publication("20240104", scale=_SCALE) == "a date"
    named = json.dumps({"strategy_id": "20240104", "sharpe": 1.2})
    assert scan_publication(named, scale=_SCALE) == "a date"
    assert export_block_reason(named, symbols=(), scale=_SCALE) == "a date"
    sheet = "initial_equity,session,sharpe\n20240104,S00001,1.2\n"
    assert scan_publication(sheet, scale=_SCALE) is None
    assert export_allowed(sheet, symbols=(), scale=_SCALE)


@windows_jails(50)
def test_grammar_runs_before_the_date_is_scrubbed_out_of_the_id(tmp_path):
    from backtester.parallel import PackMarket, run_parallel

    root = tmp_path / "batch"
    failures = (
        "20240104",
        "note_20240104",
        "01042024",
        "04012024",
        "\uff12\uff10\uff12\uff14\uff10\uff11\uff10\uff14",
        "4-Jan-24",
        "Jan-24",
        "01-04-24",
        "4 Jan 24",
        "Jan 4, 24",
        "alpha_2024-01-04_v2",
        "1QQQ",
        "Monday",
        "1704067200",
        "FY2024",
        "2024Q1",
    )
    positives = (
        "long_2x",
        "ma_200",
        "ma_20_50",
        "rsi14",
        "v2",
        "opening_drive",
        "mean_rev_3",
        "breakout_v12",
        "QQQ1",
    )
    names = failures + positives
    strategies = [_strategy(root / "strategies", name, FLAT) for name in names]
    pack, sidecar, _built = _anon_pack(root, "QQQ", name="pack", price_factor=_SCALE)
    out = root / "out"
    run_parallel(
        strategies,
        [PackMarket(str(pack), str(sidecar))],
        out,
        profile="arena",
        workers=1,
        timeout_s=120,
        with_benchmarks=False,
    )
    assert (out / "run.json").is_file()
    assert (out / "leaderboard.csv").is_file()
    jobs = json.loads((out / "run.json").read_text(encoding="utf-8"))["jobs"]
    by_id = {job["strategy_id"]: job["status"] for job in jobs}
    assert set(by_id) == set(names)
    for name in failures:
        assert by_id[name] == "error"
    for name in positives:
        assert by_id[name] == "ok"
    operator = read_leaderboard(out / "leaderboard.csv")
    designer_root = out / "designer"
    designer = read_leaderboard(designer_root / "leaderboard.csv")
    assert sorted(row["status"] for row in operator) == sorted(row["status"] for row in designer)
    assert sorted(row["status"] for row in designer) == sorted(job["status"] for job in jobs)
    shown = {row["strategy_id"]: row["status"] for row in designer}
    for name in ("long_2x", "ma_200", "ma_20_50", "rsi14", "v2", "opening_drive", "mean_rev_3", "breakout_v12"):
        assert shown[name] == "ok"
        assert any((designer_root / name).rglob("metrics.json"))
    assert shown["RANDOM1"] == "ok"
    assert "QQQ1" not in shown
    assert "1RANDOM" not in shown
    assert "alpha__v2" not in shown
    for name in failures:
        assert name not in shown
    failed = [row for row in designer if row["status"] == "error"]
    assert len(failed) == len(failures)
    for row in failed:
        folder = designer_root / row["strategy_id"]
        assert folder.is_dir()
        assert row["strategy_id"] not in failures
        errors = list(folder.rglob("error.json"))
        assert errors
        document = json.loads(errors[0].read_text(encoding="utf-8"))
        assert document["error"] == "the strategy failed"
        assert document["strategy_id"] == row["strategy_id"]
    blob = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in designer_root.rglob("*")
        if path.is_file() and path.suffix.lower() != ".png"
    )
    for name in failures:
        assert name not in blob
    assert "alpha__v2" not in blob
    assert "1RANDOM" not in blob
    assert "QQQ" not in blob
    assert "RANDOM1" in blob
    assert_designer_clean(designer_root, symbol="QQQ", scale=_SCALE)
    board = (designer_root / "leaderboard.csv").read_text(encoding="utf-8")
    assert export_allowed(board, symbols=("QQQ",), scale=_SCALE)
    assert scan_publication(board, symbols=("QQQ",), scale=_SCALE) is None
