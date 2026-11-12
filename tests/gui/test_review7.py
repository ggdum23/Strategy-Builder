"""Seventh review. Redacted designer surfaces are an allow-list."""

from __future__ import annotations

import csv
import itertools
import json
import math
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTEST_QT_API", "pyside6")
os.environ.setdefault("MPLBACKEND", "Agg")

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")
pytest.importorskip("pytestqt")

from PySide6.QtWidgets import QApplication

from backtester.engine_desk import DeskConfig, metrics as desk_metrics, run as desk_run
from backtester.gui.audience import redacted_key_allowed
from backtester.gui.backend import load_run
from backtester.gui.format import format_number
from backtester.report import write_book
from backtester.runner import execute

from test_window import _window

_CLOSE = 500.0
_SHARES = 2000.0
_EXCHANGE_PER_SHARE = 0.003
_SEC_FEE_RATE = 27.80e-6


def _bar(session, time, phase, price, **kw):
    hour, minute = (int(part) for part in time.split(":"))
    close = float(kw.get("close", price))
    return {
        "session": session,
        "time": time,
        "minute": hour * 60 + minute,
        "phase": phase,
        "open": float(kw.get("open", price)),
        "high": float(kw.get("high", max(price, close))),
        "low": float(kw.get("low", min(price, close))),
        "close": close,
        "volume": 1000.0,
        "wap": close,
        "count": 1,
        "filled": 0,
        "new_session": False,
    }


class _Long:
    def on_bar(self, row, _account):
        if row.get("time") == "09:30" and row.get("session") in {"S1", None}:
            return 1.0
        return None


class _Three:
    def on_bar(self, row, _account):
        return 3.0 if row["time"] == "09:30" else None


def _close_for_gross(gross: float) -> float:
    open_px = 100.0
    equity0 = 1_000_000.0
    shares = int(3.0 * equity0 / open_px)
    fill = open_px * (1.0 + 1.0e-4)
    cash = equity0 - shares * fill
    from backtester.costs_desk import fill_costs

    cash -= fill_costs(shares, fill, 0.0)["total"]
    return gross * cash / (shares * (1.0 - gross))


def _plant(folder: Path) -> dict:
    path = folder / "metrics.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["surprise_price"] = 123.45
    payload["S99"] = 123.45
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    return payload


def _walk_keys(value, found: set[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            found.add(str(key))
            _walk_keys(item, found)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _walk_keys(item, found)


def _surface_keys(report) -> set[str]:
    found: set[str] = set()
    for name in (
        "metrics",
        "other_fields",
        "benchmarks",
        "fees_breakdown",
        "period_returns",
        "liquidation",
        "config",
        "walkforward",
    ):
        _walk_keys(getattr(report, name), found)
    for table in (report.fills, report.episodes, report.leaderboard):
        if table is None:
            continue
        for column in table.columns:
            found.add(str(column))
        for row in table.rows:
            _walk_keys(row, found)
    for child in report.children:
        if not isinstance(child, dict):
            continue
        for key, item in child.items():
            if key == "report" and hasattr(item, "metrics"):
                found |= _surface_keys(item)
            elif key != "path":
                found.add(str(key))
                _walk_keys(item, found)
    return found


def _numbers(value, found: list[float]) -> None:
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, (int, float)):
        number = float(value)
        if math.isfinite(number):
            found.append(number)
        return
    if isinstance(value, dict):
        for item in value.values():
            _numbers(item, found)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _numbers(item, found)


def _surface_numbers(report) -> list[float]:
    found: list[float] = []
    for name in (
        "metrics",
        "other_fields",
        "benchmarks",
        "fees_breakdown",
        "period_returns",
        "liquidation",
        "config",
        "walkforward",
    ):
        _numbers(getattr(report, name), found)
    for curve in list(report.curves) + list(report.benchmark_curves):
        _numbers(curve.equity, found)
        _numbers(curve.exposure, found)
    for table in (report.fills, report.episodes, report.leaderboard):
        if table is None:
            continue
        for row in table.rows:
            _numbers(row, found)
    return found


def _export_keys(folder: Path) -> set[str]:
    found: set[str] = set()
    for path in folder.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".json", ".csv"}:
            continue
        if path.suffix.lower() == ".json":
            _walk_keys(json.loads(path.read_text(encoding="utf-8")), found)
            continue
        rows = list(csv.reader(path.read_text(encoding="utf-8").splitlines()))
        if not rows:
            continue
        if path.name == "displayed_metrics.csv":
            for row in rows[1:]:
                if row and row[0]:
                    found.add(row[0])
            continue
        for cell in rows[0]:
            if cell:
                found.add(cell)
    return found


def _reconstructions(values: list[float], target: float) -> list[str]:
    if not math.isfinite(target) or abs(target) < 5:
        return []
    hits = []
    for value in values:
        if math.isclose(value, target, rel_tol=1e-6, abs_tol=1e-4):
            hits.append(f"{value} == {target}")
    unique = list(dict.fromkeys(values))
    for left, right in itertools.combinations(unique, 2):
        if right and math.isclose(left / right, target, rel_tol=1e-6, abs_tol=1e-3):
            hits.append(f"{left} / {right} == {target}")
        if left and math.isclose(right / left, target, rel_tol=1e-6, abs_tol=1e-3):
            hits.append(f"{right} / {left} == {target}")
        if math.isclose(left * right, target, rel_tol=1e-6, abs_tol=1e-3):
            hits.append(f"{left} * {right} == {target}")
    return hits


def _targets(result: dict) -> list[float]:
    found = []
    for row in result.get("daily") or []:
        if isinstance(row, dict):
            found.append(float(row["close"]))
            found.append(abs(float(row["shares"])))
        else:
            found.append(abs(float(row[3])))
            found.append(float(row[4]))
    liquidation = result.get("liquidation") or {}
    if isinstance(liquidation, dict):
        if liquidation.get("close") is not None:
            found.append(float(liquidation["close"]))
        if liquidation.get("shares_before") is not None:
            found.append(abs(float(liquidation["shares_before"])))
    for fill in result.get("fills") or []:
        if isinstance(fill, dict):
            for key in ("price", "fill_price", "close"):
                if fill.get(key) is not None:
                    found.append(float(fill[key]))
            for key in ("shares_delta", "shares_after", "shares"):
                if fill.get(key) is not None:
                    found.append(abs(float(fill[key])))
        elif len(fill) >= 6:
            found.append(abs(float(fill[3])))
            found.append(float(fill[4]))
            found.append(float(fill[5]))
    return found


def _assert_redacted(qtbot, tmp_path: Path, folder: Path, targets: list[float], *, keep_operator: str) -> None:
    raw = json.loads((folder / "metrics.json").read_text(encoding="utf-8"))
    operator = load_run(folder, audience="operator")
    if "surprise_price" in raw:
        assert operator.metrics.get("surprise_price") == raw["surprise_price"]
    if keep_operator in raw:
        assert operator.metrics.get(keep_operator) == raw[keep_operator]

    window = _window(qtbot, tmp_path)
    page = window.results_page
    page.load_path(str(folder), audience="designer")
    QApplication.processEvents()
    report = page._report
    assert report.audience == "designer"
    assert report.redacted is True
    leaked = sorted(key for key in _surface_keys(report) if not redacted_key_allowed(key))
    assert leaked == [], leaked
    assert "surprise_price" not in report.metrics
    assert "final_shares" not in report.metrics
    assert "S99" not in _surface_keys(report)
    assert "fees_total" not in report.metrics
    if "fees_total" in raw and (raw.get("initial_equity") or raw.get("final_equity")):
        assert "cost_drag_bps" in report.metrics
    fees = report.fees_breakdown or {}
    for key in ("exchange", "sec", "commission", "counted_shares", "tier_shares", "taf", "cat"):
        assert key not in fees
    if report.liquidation:
        for key in ("close", "shares_before", "shares_after", "equity_before", "hwm"):
            assert key not in report.liquidation

    exported = tmp_path / "export" / folder.name
    page.export_to(exported)
    assert (exported / "metrics.json").is_file()
    exported_keys = _export_keys(exported)
    leaked = sorted(key for key in exported_keys if not redacted_key_allowed(key))
    assert leaked == [], leaked
    numbers = _surface_numbers(report)
    # The rounded cost drag is a coarse bps figure. Pairing it with a public
    # annual rate can land on an unrelated price, which is not the fee inversion.
    drag = report.metrics.get("cost_drag_bps") if isinstance(report.metrics, dict) else None
    ratio_numbers = numbers
    if isinstance(drag, (int, float)) and not isinstance(drag, bool):
        ratio_numbers = [number for number in numbers if not math.isclose(number, float(drag))]
    for target in targets:
        hits = _reconstructions(ratio_numbers, target)
        if (
            isinstance(drag, (int, float))
            and not isinstance(drag, bool)
            and math.isfinite(target)
            and abs(target) >= 5
            and math.isclose(float(drag), target, rel_tol=1e-6, abs_tol=1e-4)
        ):
            hits.append(f"cost_drag_bps {drag} == {target}")
        assert hits == [], (target, hits[:8])
    for key in ("cash_interest", "financing_cost", "dividends_net", "dividend_per_share"):
        assert key not in report.metrics
    for key in (
        "accounting",
        "price_basis",
        "total_return_method",
        "series_kind",
        "cash_rate_annual",
        "financing_rate_annual",
        "withholding_rate",
        "accrual",
        "sessions_per_year",
        "dividends_applied",
    ):
        if key in raw:
            assert key in report.metrics


def test_redacted_repro_cannot_rebuild_close_or_shares(qtbot, tmp_path):
    folder = tmp_path / "repro"
    folder.mkdir()
    exchange = _EXCHANGE_PER_SHARE * _SHARES
    sec = _SEC_FEE_RATE * _SHARES * _CLOSE
    metrics = {
        "profile": "desk",
        "symbol": "QQQ",
        "anonymized": True,
        "total_return": 0.0,
        "final_equity": 1_000_000,
        "initial_equity": 1_000_000,
        "final_shares": _SHARES,
        "exposure": 1.0,
        "fees_total": 8.25,
        "fees_breakdown": {
            "commission": 1.0,
            "exchange": exchange,
            "sec": sec,
            "counted_shares": _SHARES,
            "tier_shares": _SHARES,
            "total": 8.25,
        },
        "liquidation": {
            "session": "S1",
            "time": "16:00",
            "phase": "post",
            "close": _CLOSE,
            "equity_before": 1_000_000,
            "hwm": 1_000_000,
            "shares_before": _SHARES,
        },
        "cost_basis": {"share_scale": 7.777, "anonymized": True},
        "surprise_price": _CLOSE,
    }
    (folder / "metrics.json").write_text(json.dumps(metrics) + "\n", encoding="utf-8")
    (folder / "equity_curve.csv").write_text(
        "session,equity,exposure,shares,close\nS00001,1000000,1.0,2000,500\n",
        encoding="utf-8",
    )
    (folder / "equity.csv").write_bytes((folder / "equity_curve.csv").read_bytes())
    (folder / "fills.csv").write_text(
        "session,time,phase,minute,shares_delta,price,fill_price,fees,exchange,sec,counted_shares,tier_shares,shares_after,kind\n"
        "S00001,09:31,rth,571,2000,500,500,1.25,6,0.0278,2000,2000,2000,strategy\n",
        encoding="utf-8",
    )
    _assert_redacted(qtbot, tmp_path, folder, [_CLOSE, _SHARES], keep_operator="final_shares")
    loaded = load_run(folder, audience="designer")
    # The two published inversions have no inputs left on the redacted book.
    assert "final_shares" not in loaded.metrics
    fees = loaded.fees_breakdown or {}
    assert "sec" not in fees and "exchange" not in fees
    price = sec * _EXCHANGE_PER_SHARE / (_SEC_FEE_RATE * exchange)
    assert math.isclose(price, _CLOSE)
    equity = loaded.metrics["final_equity"]
    exposure = loaded.curves[0].exposure[0]
    assert not math.isclose(exposure * equity / _SHARES, _CLOSE) or "final_shares" not in loaded.metrics


def test_real_desk_and_arena_reports_stay_on_the_allow_list(qtbot, tmp_path):
    import pandas as pd

    liquidated = tmp_path / "liquidated"
    bars = [
        _bar("S1", "09:30", "rth", 100),
        _bar("S1", "09:31", "rth", 100),
        _bar("S1", "15:59", "rth", 100),
        _bar("S1", "16:00", "post", 100, close=20, low=20),
        _bar("S2", "09:30", "rth", 20),
    ]
    result = desk_run(_Long(), bars, DeskConfig())
    assert result["liquidation"]["shares_before"] != 0
    write_book(liquidated, "desk", result, desk_metrics(result), None)
    _plant(liquidated)
    _assert_redacted(qtbot, tmp_path, liquidated, _targets(result), keep_operator="final_shares")

    called = tmp_path / "margin"
    close = _close_for_gross(3.16)
    margin_bars = [
        _bar("S1", "09:30", "rth", 100.0),
        _bar("S1", "09:31", "rth", 100.0, close=close),
    ]
    margin = desk_run(_Three(), margin_bars, DeskConfig())
    assert margin["margin_calls_queued"] >= 1
    write_book(called, "desk", margin, desk_metrics(margin), None)
    _plant(called)
    _assert_redacted(qtbot, tmp_path, called, _targets(margin), keep_operator="final_shares")

    rows = []
    for day in (0, 1):
        for minute in (570, 571):
            rows.append(
                {
                    "date": f"2026-03-0{2 + day}",
                    "day": day,
                    "weekday": 0,
                    "minute": minute,
                    "time": f"{minute // 60:02d}:{minute % 60:02d}",
                    "session": "rth",
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.0,
                    "close": 100.0 + day,
                    "volume": 1000.0,
                    "wap": 100.0,
                    "count": 1,
                    "filled": 0,
                    "short_session": 0,
                }
            )
    frame = pd.DataFrame(rows)
    arena_result, arena_metrics = execute(_Long(), frame, "arena")
    arena = tmp_path / "arena"
    write_book(arena, "arena", arena_result, arena_metrics, frame)
    _plant(arena)
    _assert_redacted(qtbot, tmp_path, arena, _targets(arena_result), keep_operator="total_return")


def _cell_text(table) -> list[str]:
    model = table.model()
    if model is None:
        return []
    texts = []
    for row in range(model.rowCount()):
        for column in range(model.columnCount()):
            texts.append(str(model.data(model.index(row, column)) or ""))
    return texts


def test_accounting_dollars_are_dropped_and_operator_shows_them(qtbot, tmp_path):
    """Interest, financing, and a per-share dividend invert; the settings do not.

    Idle cash is ``cash_interest * 252 / cash_rate``. Borrowed notional is
    ``financing_cost * 252 / financing_rate``. Shares are
    ``dividends_net / (per_share * (1 - withholding))`` for a positive long.
    """
    shares = 2000.0
    close = 500.0
    equity = 1_500_000.0
    idle = equity - shares * close
    cash_rate = 0.05
    financing_rate = 0.07
    borrowed = 600_000.0
    withholding = 0.15
    per_share = 2.50
    interest = cash_rate / 252.0 * idle
    financing = financing_rate / 252.0 * borrowed
    dividends = shares * per_share * (1.0 - withholding)
    assert math.isclose(interest * 252.0 / cash_rate, idle)
    assert math.isclose(equity - idle, shares * close)
    assert math.isclose(financing * 252.0 / financing_rate, borrowed)
    assert math.isclose(dividends / (per_share * (1.0 - withholding)), shares)

    accounting = {
        "price_basis": "raw",
        "total_return_method": "cash_dividends",
        "series_kind": "raw",
        "cash_rate_annual": cash_rate,
        "financing_rate_annual": financing_rate,
        "withholding_rate": withholding,
        "accrual": "session_boundary_1_252",
        "sessions_per_year": 252,
        "dividends_applied": True,
    }
    metrics = {
        "profile": "arena",
        "symbol": "QQQ",
        "anonymized": True,
        "total_return": 0.0,
        "final_equity": equity,
        "initial_equity": equity,
        "exposure": 1.0,
        "fees_total": 1.25,
        "cash_interest": interest,
        "financing_cost": financing,
        "dividends_net": dividends,
        "dividend_per_share": per_share,
        **accounting,
        "accounting": accounting,
    }
    folder = tmp_path / "accounting"
    folder.mkdir()
    (folder / "metrics.json").write_text(json.dumps(metrics) + "\n", encoding="utf-8")
    (folder / "equity_curve.csv").write_text(
        "session,equity,exposure\nS00001,1500000,1.0\n",
        encoding="utf-8",
    )
    _plant(folder)

    window = _window(qtbot, tmp_path)
    page = window.results_page
    page.load_path(str(folder), audience="operator")
    QApplication.processEvents()
    shown = _cell_text(page.accounting)
    assert "Price basis" in shown
    assert "raw" in shown
    assert "Cash interest" in shown
    assert format_number("money", interest) in shown
    assert "Financing" in shown
    assert format_number("money", financing) in shown
    assert "Dividends" in shown
    assert format_number("money", dividends) in shown
    assert "Dividends applied" in shown
    assert "yes" in shown
    assert "session_boundary_1_252" in shown

    _assert_redacted(qtbot, tmp_path, folder, [close, shares, idle, borrowed], keep_operator="cash_interest")
    page.load_path(str(folder), audience="designer")
    QApplication.processEvents()
    hidden = _cell_text(page.accounting)
    assert "Price basis" in hidden
    assert "raw" in hidden
    assert "Cash rate" in hidden
    assert "session_boundary_1_252" in hidden
    assert "Dividends applied" in hidden
    for label in ("Cash interest", "Financing"):
        assert label not in hidden
    assert "Dividends" not in hidden
    for amount in (interest, financing, dividends, per_share, idle, borrowed, close, shares):
        assert format_number("money", amount) not in hidden


def _price_frame(price: float):
    import pandas as pd

    rows = []
    for day in (0, 1):
        for minute in (570, 571):
            rows.append(
                {
                    "date": f"2026-03-0{2 + day}",
                    "day": day,
                    "weekday": 0,
                    "minute": minute,
                    "time": f"{minute // 60:02d}:{minute % 60:02d}",
                    "session": "rth",
                    "open": price,
                    "high": price + 1.0,
                    "low": price - 1.0,
                    "close": price,
                    "volume": 1000.0,
                    "wap": price,
                    "count": 1,
                    "filled": 0,
                    "short_session": 0,
                }
            )
    return pd.DataFrame(rows)


class _Hold:
    def on_bar(self, _row, _account):
        return 1.0


def _designer_keys(root: Path) -> set[str]:
    found: set[str] = set()
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix == ".json":
            _walk_keys(json.loads(path.read_text(encoding="utf-8")), found)
        elif path.suffix == ".csv":
            rows = list(csv.reader(path.read_text(encoding="utf-8").splitlines()))
            if rows:
                found.update(cell for cell in rows[0] if cell)
    return found


def test_published_designer_books_use_the_allow_list(qtbot, tmp_path):
    """Anonymized Desk and Arena books go through run_books and publish_designer.

    Desk at price 777.7 and share factor 7.777 recovers that factor from
    ``exchange / (0.003 * final_shares)`` on the operator book. The designer
    tree has neither input. A planted ``S99`` does not survive the screen or
    the export. A normal designer metrics file still exports at the scales
    whose digits collide with ordinary rates.
    """
    from backtester.gui.audience import export_allowed
    from backtester.runner import run_books, write_books

    frame = _price_frame(777.7)
    for profile in ("desk", "arena"):
        out = tmp_path / profile
        books = run_books(
            _Hold,
            frame,
            profile,
            share_factor=7.777,
            cash_rate_annual=0.05,
        )
        write_books(out, profile, "QQQ", 1, frame, books, anonymized=True)
        operator_bytes = {
            path.relative_to(out): path.read_bytes()
            for path in out.rglob("*")
            if path.is_file() and "designer" not in path.parts
        }
        designer_root = out / "designer"
        leaked = sorted(key for key in _designer_keys(designer_root) if not redacted_key_allowed(key))
        assert leaked == [], (profile, leaked)
        designer_metrics = json.loads((designer_root / "metrics.json").read_text(encoding="utf-8"))
        blob = json.dumps(designer_metrics)
        for banned in ("final_shares", "fees_total", "fees_breakdown", "exchange", "fill_price", "traded_notional"):
            assert banned not in designer_metrics
            assert f'"{banned}"' not in blob
        assert "cost_drag_bps" in designer_metrics
        if profile == "desk":
            assert math.isclose(designer_metrics["borrow_rate_annual"], 0.0025)
            assert math.isclose(designer_metrics["cash_rate_annual"], 0.05)
            assert math.isclose(designer_metrics["financing_rate_annual"], 0.07)
        assert "equity" in (designer_root / "equity_curve.csv").read_text(encoding="utf-8").splitlines()[0]
        operator_curve = (out / "equity_curve.csv").read_text(encoding="utf-8").splitlines()
        designer_curve = (designer_root / "equity_curve.csv").read_text(encoding="utf-8").splitlines()
        operator_header = operator_curve[0].split(",")
        designer_header = designer_curve[0].split(",")
        operator_equity = [row.split(",")[operator_header.index("equity")] for row in operator_curve[1:] if row]
        designer_equity = [row.split(",")[designer_header.index("equity")] for row in designer_curve[1:] if row]
        assert designer_equity == operator_equity
        fills = (out / "fills.csv").read_text(encoding="utf-8").splitlines()[0]
        assert "fill_price" in fills.split(",")
        designer_fills = (designer_root / "fills.csv").read_text(encoding="utf-8").splitlines()[0]
        assert "fill_price" not in designer_fills.split(",")
        text = (designer_root / "metrics.json").read_text(encoding="utf-8")
        for scale in (0.25, 0.5, 0.7, 2, 7.777):
            assert export_allowed(text, symbols=("QQQ",), scale=scale), (profile, scale)
        assert not export_allowed(
            json.dumps({"message": "build2_1e\u22127"}, ensure_ascii=False), symbols=(), scale=1e-7
        )
        assert not export_allowed(
            json.dumps({"message": "\uff17\uff0e\uff17\uff17\uff17"}, ensure_ascii=False), symbols=(), scale=7.777
        )

        planted = json.loads((designer_root / "metrics.json").read_text(encoding="utf-8"))
        planted["S99"] = 777.7
        (designer_root / "metrics.json").write_text(json.dumps(planted) + "\n", encoding="utf-8")
        _assert_redacted(qtbot, tmp_path, out, [777.7], keep_operator="final_shares" if profile == "desk" else "total_return")
        assert operator_bytes == {
            path.relative_to(out): path.read_bytes()
            for path in out.rglob("*")
            if path.is_file() and "designer" not in path.parts
        }

    desk = json.loads((tmp_path / "desk" / "metrics.json").read_text(encoding="utf-8"))
    exchange = desk["fees_breakdown"]["exchange"]
    final_shares = desk["final_shares"]
    assert final_shares
    assert math.isclose(exchange / (_EXCHANGE_PER_SHARE * final_shares), 7.777, rel_tol=1e-6)
    assert "QQQ" not in (tmp_path / "desk" / "designer" / "metrics.json").read_text(encoding="utf-8")
