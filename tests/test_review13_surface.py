"""Thirteenth review. A strategy id that is a calendar date fails that one job."""

from __future__ import annotations

import json

import pytest

from backtester import calendar_text
from backtester.calendar_text import contains_calendar_date, strip_calendar_dates
from backtester.gui import audience
from backtester.gui.audience import export_allowed, export_block_reason, scan_publication, scrub_text
from backtester.report import _designer_value_error, assert_designer_clean, designer_component_problem, read_leaderboard
from test_parallel import FLAT, _anon_pack, _strategy

# Every spelling the publisher used to accept, plus the controls that stay.
# ``day 2024`` is a date. A decimal year is still a number.
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
)
KEPT = (
    "opening_drive",
    "2024.50",
    "0.2024",
)
_SCALE = 1.1


def _date_hits(text: str) -> tuple[bool, bool, bool, bool, bool]:
    """The five gates, as a date verdict. A price is not a date."""
    component = designer_component_problem(text, symbols=(), scale=_SCALE) == "a calendar date"
    value = _designer_value_error("caption", text, "") == "a calendar date"
    scanned = scan_publication(text, scale=_SCALE) == "a date"
    scrubbed = scrub_text(text, (), scale=_SCALE) != text
    exported = export_block_reason(text, symbols=(), scale=_SCALE) == "a date"
    return (component, value, scanned, scrubbed, exported)


def test_the_five_gates_use_one_date_predicate():
    assert audience.contains_calendar_date is contains_calendar_date
    assert audience.strip_calendar_dates is strip_calendar_dates
    from backtester import report

    assert report.contains_calendar_date is contains_calendar_date
    assert not hasattr(audience, "_DATE_PATTERNS")
    assert not hasattr(audience, "_EXPORT_DATE_PATTERNS")
    assert calendar_text.DATE_PATTERNS[-1].search("today 2024")
    assert calendar_text.DATE_PATTERNS[-1].search("holiday 2024")
    assert calendar_text.DATE_PATTERNS[-1].search("day 2024")
    assert calendar_text.DATE_PATTERNS[-1].search("Day 2024")
    assert not hasattr(audience, "_GLUED_YEAR")
    assert not contains_calendar_date("2024.50")
    assert not contains_calendar_date("0.2024")


@pytest.mark.parametrize("text", DATES)
def test_every_gate_rejects_the_date(text):
    assert _date_hits(text) == (True, True, True, True, True)
    assert contains_calendar_date(text)
    assert "2024" not in scrub_text(text, ())
    assert export_block_reason(text, symbols=(), scale=_SCALE) == "a date"
    assert scan_publication(text, scale=_SCALE) == "a date"
    assert designer_component_problem(text, symbols=("QQQ",), scale=_SCALE) == "a calendar date"
    assert _designer_value_error("caption", text, "QQQ") == "a calendar date"
    assert export_allowed(text, symbols=(), scale=_SCALE) is False


@pytest.mark.parametrize("text", KEPT)
def test_every_gate_keeps_the_control(text):
    assert _date_hits(text) == (False, False, False, False, False)
    assert contains_calendar_date(text) is False
    assert scrub_text(text, (), scale=_SCALE) == text
    assert export_block_reason(text, symbols=(), scale=_SCALE) != "a date"
    assert scan_publication(text, scale=_SCALE) != "a date"
    assert designer_component_problem(text, symbols=(), scale=_SCALE) != "a calendar date"


def test_gates_agree_on_a_shared_corpus():
    corpus = list(DATES) + list(KEPT)
    corpus.extend(f"note {text} tail" for text in DATES + KEPT)
    corpus.append("strategy_id,note\nopening_drive,04-Jan-2024\n")
    corpus.append("strategy_id,sharpe\nopening_drive,1.2\n")
    quoted = '{"note": "holiday 2024"}\n'
    assert designer_component_problem(quoted, symbols=(), scale=_SCALE) == "an unsafe name"
    assert _designer_value_error("caption", quoted, "") == "a calendar date"
    assert scan_publication(quoted, scale=_SCALE) == "a date"
    assert export_block_reason(quoted, symbols=(), scale=_SCALE) == "a date"
    assert contains_calendar_date(quoted)
    for text in corpus:
        hits = _date_hits(text)
        assert len(set(hits)) == 1, (text, hits)
        assert hits[0] is contains_calendar_date(text)
        assert (scrub_text(text, (), scale=_SCALE) != text) is contains_calendar_date(text)
        assert (strip_calendar_dates(text) != text) is contains_calendar_date(text)
        assert scrub_text(text, ()) == strip_calendar_dates(text)


def test_slash_date_stays_an_unsafe_name():
    assert designer_component_problem("2024/01/04", symbols=(), scale=_SCALE) == "an unsafe name"
    assert contains_calendar_date("2024/01/04")
    assert scan_publication("2024/01/04", scale=_SCALE) == "a date"
    assert export_block_reason("2024/01/04", symbols=(), scale=_SCALE) == "a date"


def test_date_id_fails_one_job_and_the_sibling_publishes(tmp_path):
    from backtester.parallel import PackMarket, run_parallel

    root = tmp_path / "batch"
    names = DATES + ("opening_drive",)
    strategies = [_strategy(root / "strategies", name, FLAT) for name in names]
    pack, sidecar, _built = _anon_pack(root, "QQQ", name="pack", price_factor=_SCALE)
    out = root / "out"
    run_parallel(
        strategies,
        [PackMarket(str(pack), str(sidecar))],
        out,
        profile="arena",
        workers=1,
        timeout_s=60,
        with_benchmarks=False,
    )
    assert (out / "run.json").is_file()
    assert (out / "leaderboard.csv").is_file()
    jobs = json.loads((out / "run.json").read_text(encoding="utf-8"))["jobs"]
    by_id = {job["strategy_id"]: job["status"] for job in jobs}
    assert by_id["opening_drive"] == "ok"
    assert {by_id[name] for name in DATES} == {"error"}
    operator = read_leaderboard(out / "leaderboard.csv")
    designer_root = out / "designer"
    designer = read_leaderboard(designer_root / "leaderboard.csv")
    assert sorted(row["status"] for row in operator) == sorted(row["status"] for row in designer)
    assert sorted(row["status"] for row in designer) == sorted(job["status"] for job in jobs)
    shown = {row["strategy_id"] for row in designer}
    assert "opening_drive" in shown
    assert shown.isdisjoint(DATES)
    failed = [row for row in designer if row["status"] == "error"]
    assert len(failed) == len(DATES)
    for row in failed:
        folder = designer_root / row["strategy_id"]
        assert folder.is_dir()
        assert row["strategy_id"] not in DATES
        assert "2024" not in row["strategy_id"]
        errors = list(folder.rglob("error.json"))
        assert errors
        for path in errors:
            document = json.loads(path.read_text(encoding="utf-8"))
            assert document["error"] == "the strategy failed"
            assert document["strategy_id"] == row["strategy_id"]
            assert document["strategy_id"] not in DATES
    names_on_disk = {path.name for path in designer_root.rglob("*")}
    assert names_on_disk.isdisjoint(DATES)
    blob = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in designer_root.rglob("*")
        if path.is_file() and path.suffix.lower() != ".png"
    )
    for name in DATES:
        assert name not in blob
    assert any((designer_root / "opening_drive").rglob("metrics.json"))
    assert_designer_clean(designer_root, symbol="QQQ", scale=_SCALE)
    board_text = (designer_root / "leaderboard.csv").read_text(encoding="utf-8")
    assert export_allowed(board_text, symbols=("QQQ",), scale=_SCALE)
    assert scan_publication(board_text, symbols=("QQQ",), scale=_SCALE) is None
