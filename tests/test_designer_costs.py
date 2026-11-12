"""Designer runs bill a flat fee. The operator invoice stays the real schedule."""

from __future__ import annotations

import ast
import csv
import gzip
import hashlib
import io
import json
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import pytest

from backtester import costs as arena_costs
from backtester import costs_desk as desk_costs
from backtester import costs_designer as designer_costs
from backtester import engine_arena, engine_desk
from backtester.costs_designer import (
    ARENA_FLAT_BPS,
    DESK_FLAT_BPS,
    DESIGNER_INITIAL_EQUITY,
    DesignerCostModel,
    for_profile,
    label,
)
from backtester.data import load_named, slice_walkforward
from backtester.designer_run import execute as designer_execute
from backtester.engine_desk import BuyAndHold, DeskConfig
from backtester.pack import build_pack, factor_mac, load_pack, load_sidecar
from backtester.registry import entry_from_inspection, save_registry, upsert_series
from backtester.report import assert_designer_clean, load_report
from backtester.runner import execute, run_named, run_pack, walk_forward, walk_pack
from windows_open import windows_jails

ROOT = Path(__file__).resolve().parents[1]
COSTS_SHA256 = "a3b0a522d8bf469001e75aaf274b0c5d3cbef624a68ed9fb94d29298110fe281"
ET = timezone(timedelta(hours=-4))
SCALES = (0.25, 1.0, 1.7, 7.777)


class BuyThenSell:
    """Target 1.0 at 09:30 and flat at 09:31. The next bar fills each order."""

    WANTS_EXTENDED_HOURS = False

    def on_bar(self, bar, _state):
        phase = bar["phase"] if "phase" in bar else bar["session"]
        if phase != "rth":
            return None
        if "minute" in bar and bar["minute"] is not None:
            minute = int(bar["minute"])
        else:
            hour, minute_of_hour = (int(part) for part in str(bar["time"]).split(":"))
            minute = hour * 60 + minute_of_hour
        if minute == 9 * 60 + 30:
            return 1.0
        if minute == 9 * 60 + 31:
            return 0.0
        return None


def _round_tenth(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def _reference_average(sec_rate, taf_rate, taf_max) -> tuple[Decimal, Decimal, Decimal]:
    """Buy bps, sell bps, and their average at the documented $100 / 10,000-share fill."""
    shares = Decimal(10_000)
    notional = Decimal(1_000_000)
    commission = shares * Decimal("0.0035")
    exchange = shares * Decimal("0.0030")
    clearing = shares * Decimal("0.00020")
    passthrough = commission * (Decimal("0.000175") + Decimal("0.00056"))
    cat = shares * Decimal("0.000003")
    subtotal = commission + exchange + clearing + passthrough + cat
    buy_bps = subtotal / notional * Decimal(10_000)
    sec = Decimal(str(sec_rate)) * notional
    taf = min(shares * Decimal(str(taf_rate)), Decimal(str(taf_max)))
    sell_bps = (subtotal + sec + taf) / notional * Decimal(10_000)
    return buy_bps, sell_bps, (buy_bps + sell_bps) / 2


def test_flat_bps_is_the_documented_round_half_up():
    assert arena_costs.COMMISSION_TIERS[0] == (300_000, 0.0035)
    assert desk_costs.COMMISSION_TIERS[0] == (300_000, 0.0035)
    assert arena_costs.SLIPPAGE_BPS == desk_costs.SLIPPAGE_BPS == designer_costs.SLIPPAGE_BPS == 1.0
    commission = Decimal(10_000) * Decimal("0.0035")
    assert commission == Decimal(35)
    assert commission > Decimal(str(arena_costs.COMMISSION_MIN))
    assert commission < Decimal(str(arena_costs.COMMISSION_MAX_FRAC)) * Decimal(1_000_000)
    passthrough = commission * (
        Decimal(str(arena_costs.PASS_THROUGH_NYSE)) + Decimal(str(arena_costs.PASS_THROUGH_FINRA))
    )
    assert passthrough == Decimal("0.025725")
    arena_buy, arena_sell, arena_avg = _reference_average(
        arena_costs.SEC_FEE_RATE, arena_costs.TAF_PER_SHARE, arena_costs.TAF_MAX
    )
    desk_buy, desk_sell, desk_avg = _reference_average(
        desk_costs.SEC_FEE_RATE, desk_costs.TAF_PER_SHARE, desk_costs.TAF_MAX
    )
    assert arena_buy == desk_buy == Decimal("0.67055725")
    assert arena_sell == Decimal("0.89605725")
    assert desk_sell == Decimal("0.96515725")
    assert arena_avg == Decimal("0.78330725")
    assert desk_avg == Decimal("0.81785725")
    assert _round_tenth(arena_avg) == Decimal("0.8")
    assert _round_tenth(desk_avg) == Decimal("0.8")
    assert ARENA_FLAT_BPS == 0.8
    assert DESK_FLAT_BPS == 0.8
    assert label("arena") == "designer cost model: 0.8 bp flat"
    assert label("desk") == label("arena")
    assert Decimal(10_000) * Decimal(str(arena_costs.TAF_PER_SHARE)) < Decimal(str(arena_costs.TAF_MAX))
    assert Decimal(10_000) * Decimal(str(desk_costs.TAF_PER_SHARE)) < Decimal(str(desk_costs.TAF_MAX))


def test_costs_py_bytes_are_unchanged():
    digest = hashlib.sha256((ROOT / "src" / "backtester" / "costs.py").read_bytes()).hexdigest()
    assert digest == COSTS_SHA256


def test_designer_modules_do_not_import_or_call_the_per_share_schedules():
    for name in ("costs_designer.py", "designer_run.py"):
        path = ROOT / "src" / "backtester" / name
        text = path.read_text(encoding="utf-8")
        assert "share_scale" not in text
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                imported = [base] + [alias.name for alias in node.names]
            else:
                imported = []
            for item in imported:
                tail = item.split(".")[-1]
                assert tail not in {"costs", "costs_desk"}
            if isinstance(node, ast.Name):
                assert node.id not in {"share_scale", "share_factor"}
            if isinstance(node, ast.Call):
                for keyword in node.keywords:
                    assert keyword.arg not in {"share_scale", "share_factor"}
                func = node.func
                called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                assert called not in {"fill_costs", "tiered_commission", "slipped_price"}
    designer = (ROOT / "src" / "backtester" / "designer_run.py").read_text(encoding="utf-8")
    assert designer.count("share_factor") == 1
    assert 'pop("share_factor", None)' in designer
    model = for_profile("arena")
    with pytest.raises(TypeError):
        model.fill_costs(10, 100.0, 0.0, share_scale=1.7)


def _fee(delta, price, bps=0.8):
    return abs(delta) * price * bps * 1e-4


def test_flat_fee_is_charged_on_buys_and_on_sells():
    from test_engine import Scripted, make_bars

    bars = make_bars(days=1, rth_minutes=(570, 571, 572), price=lambda _i: 100.0)
    plan = {(0, 570): 1.0, (0, 571): 0.0}
    model = for_profile("arena")
    result = engine_arena.run(Scripted(plan), bars, cost_model=model, share_scale=7.777)
    signs = []
    assert result["fees"]["commission"] == 0.0
    assert result["fees"]["taf"] == 0.0
    assert result["fees"]["sec"] == 0.0
    assert "share_scale" not in result
    for day, minute, session, delta, _open, fill_price, fees, _after in result["fills"]:
        del day, minute, session
        signs.append(delta > 0)
        assert fees == _fee(delta, fill_price)
    assert True in signs and False in signs
    again = engine_arena.run(Scripted(plan), bars, cost_model=model, share_scale=0.25)
    assert again["daily"] == result["daily"]
    assert again["fees"] == result["fees"]
    assert again["fills"] == result["fills"]


def _desk_bar(session, time, price, **kw):
    hour, minute = (int(part) for part in time.split(":"))
    close = float(kw.get("close", price))
    return {
        "session": session,
        "time": time,
        "minute": hour * 60 + minute,
        "phase": kw.get("phase", "rth"),
        "open": float(price),
        "high": max(float(price), close),
        "low": min(float(price), close, float(kw.get("low", price))),
        "close": close,
        "volume": 1000.0,
        "wap": close,
        "count": 10,
        "filled": 0,
        "new_session": kw.get("new_session", False),
    }


class _Fn:
    def __init__(self, fn):
        self.fn = fn

    def on_bar(self, row, account):
        return self.fn(row, account)


def _designer_desk(signal, bars):
    return engine_desk.run(_Fn(signal), bars, DeskConfig(cost_model=for_profile("desk")))


def test_desk_margin_and_liquidation_use_the_flat_fee():
    model = for_profile("desk")
    equity0 = DESIGNER_INITIAL_EQUITY
    open_px = 100.0
    shares = int(3.0 * equity0 / open_px)
    fill = model.slipped_price(open_px, shares)
    cash = equity0 - shares * fill - model.fill_costs(shares, fill, 0.0)["total"]
    gross = 3.16
    close = gross * cash / (shares * (1.0 - gross))
    bars = [
        _desk_bar("S1", "09:30", 100.0),
        _desk_bar("S1", "09:31", 100.0, close=close),
        _desk_bar("S2", "09:30", close, new_session=True),
    ]

    def signal(row, _account):
        return 3.0 if row["time"] == "09:30" and row["session"] == "S1" else None

    margin = _designer_desk(signal, bars)
    assert margin["margin_calls_queued"] == 1
    assert margin["margin_calls_filled"] == 1
    margin_fill = next(row for row in margin["fills"] if row["kind"] == "margin")
    assert margin_fill["fees"] == _fee(margin_fill["shares_delta"], margin_fill["fill_price"])
    assert "share_factor" not in margin["config"]

    shares = int(equity0 / open_px)
    fill = model.slipped_price(open_px, shares)
    cash = equity0 - shares * fill - model.fill_costs(shares, fill, 0.0)["total"]
    crash = 200_000.0
    crashed_close = (crash - cash) / shares
    crash_bars = [
        _desk_bar("S1", "09:30", 100.0),
        _desk_bar("S1", "15:59", 100.0, close=crashed_close),
    ]

    def hold(row, _account):
        return 1.0 if row["time"] == "09:30" else None

    liquidated = _designer_desk(hold, crash_bars)
    assert liquidated["disqualified"] is True
    assert liquidated["liquidation"]["equity_before"] < 0.30 * liquidated["liquidation"]["hwm"]
    liq = next(row for row in liquidated["fills"] if row["kind"] == "liquidation")
    assert liq["fees"] == _fee(liq["shares_delta"], liq["fill_price"])
    assert liq["shares_delta"] < 0


def _write_series(tmp_path, days=4):
    start = datetime(2024, 3, 4)
    header = [
        "timestamp_et", "epoch_utc", "open", "high", "low", "close", "volume", "wap", "count", "filled", "session",
    ]
    lines = [",".join(header)]
    for offset in range(days):
        day = start + timedelta(days=offset)
        for minute in (9 * 60 + 30, 9 * 60 + 31, 9 * 60 + 32):
            stamp = (day + timedelta(minutes=minute)).replace(tzinfo=ET)
            price = 100.0 + offset
            lines.append(
                ",".join(
                    str(value)
                    for value in (
                        stamp.isoformat(),
                        int(stamp.timestamp()),
                        price,
                        price + 0.5,
                        price - 0.5,
                        price + 0.25,
                        1000,
                        price,
                        4,
                        0,
                        "rth",
                    )
                )
            )
    path = tmp_path / "src" / "NIK_1min_filled_2024-03.csv.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")
    loaded = load_named(path.parent, "NIK")
    entry = entry_from_inspection(loaded.inspection, source_root=str(path.parent), profile="desk")
    registry = tmp_path / "registry.json"
    save_registry(registry, upsert_series({"schema_version": 1, "series": []}, entry))
    return path.parent, registry


def _build(tmp_path, src, registry, *, symbol="NIK", price_factor=1.7, volume_factor=0.4):
    pack = tmp_path / f"pack-{symbol}-{price_factor}"
    sidecar = tmp_path / f"side-{symbol}-{price_factor}.json"
    build_pack(
        src,
        symbol,
        pack,
        sidecar,
        anonymize=True,
        registry_path=registry,
        profile="desk",
        price_factor=price_factor,
        volume_factor=volume_factor,
    )
    return pack, sidecar


def _rewrite(sidecar, *, allow_scale_mismatch=False, resign=False, **changes):
    document = json.loads(Path(sidecar).read_text(encoding="utf-8"))
    document.update(changes)
    if resign:
        # The operator holds the key, so an intentional billing-scale edit can
        # be signed again. A tamper that does not resign is refused.
        document["factor_mac"] = factor_mac(bytes.fromhex(document["factor_key"]), document)
    Path(sidecar).write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return load_sidecar(sidecar, allow_scale_mismatch=allow_scale_mismatch)


def _tree(root: Path) -> dict[str, bytes]:
    found = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            found[str(path.relative_to(root))] = path.read_bytes()
    return found


def _assert_no_scale(tree: dict[str, bytes]) -> None:
    for name, blob in tree.items():
        if name.endswith(".png"):
            continue
        text = blob.decode("utf-8")
        assert "share_scale" not in text
        assert "share_factor" not in text
        assert "price_factor" not in text
        assert "factor_key" not in text
        assert "factor_mac" not in text
        assert "borrow_rate_source" not in text


@pytest.mark.parametrize("profile", ["arena", "desk"])
@windows_jails(8)
def test_designer_tree_is_identical_across_share_scales(tmp_path, profile):
    src, registry = _write_series(tmp_path)
    pack, sidecar = _build(tmp_path, src, registry)
    trees = []
    operator_fees = []
    for scale in SCALES:
        _rewrite(sidecar, allow_scale_mismatch=True, resign=True, share_scale=scale)
        out = tmp_path / f"{profile}-{scale}"
        payload = run_pack(
            BuyThenSell,
            pack,
            sidecar,
            profile,
            out,
            with_benchmarks=False,
            allow_scale_mismatch=True,
        )
        basis = payload["cost_basis"]
        assert basis["share_scale"] == scale
        if scale == 1.7:
            assert basis["per_share_fees"] == "real_share_counts"
            assert "price_factor" not in basis
        else:
            assert basis["per_share_fees"] == "billing_override"
            assert basis["price_factor"] == 1.7
            assert basis["share_scale"] == scale
        assert "cost_model" not in payload
        designer = json.loads((out / "designer" / "metrics.json").read_text(encoding="utf-8"))
        assert designer["cost_model"] == "designer cost model: 0.8 bp flat"
        assert designer["flat_fee_bps"] == 0.8
        assert designer["initial_equity"] == DESIGNER_INITIAL_EQUITY
        assert "cost_basis" not in designer
        assert "share_factor" not in designer
        comparison = json.loads((out / "cost_comparison.json").read_text(encoding="utf-8"))
        assert comparison["label"] == designer["cost_model"]
        assert "share_scale" not in json.dumps(comparison)
        block = comparison["books"]["strategy"]
        assert block["difference"]["total_return"] == (
            block["designer_cost"]["total_return"] - block["real_fees"]["total_return"]
        )
        assert "final_equity" not in block["difference"]
        assert "final_equity" in block["real_fees"]
        assert "final_equity" in block["designer_cost"]
        from backtester.designer_allow import cost_drag_bps

        fills = list(csv.DictReader((out / "designer" / "fills.csv").open(encoding="utf-8")))
        assert fills
        withheld = {"shares_delta", "fill_price", "fees", "shares", "price", "open"}
        assert withheld.isdisjoint(fills[0])
        assert designer["cost_drag_bps"] == cost_drag_bps(
            block["designer_cost"]["fees_total"], designer["initial_equity"]
        )
        assert "fees_total" not in designer
        tree = _tree(out / "designer")
        _assert_no_scale(tree)
        assert_designer_clean(out / "designer", symbol="NIK")
        trees.append(tree)
        operator_fees.append(payload["fees_total"])
    for tree in trees[1:]:
        assert tree == trees[0]
    assert len(set(operator_fees)) == len(SCALES)


@windows_jails(4)
def test_designer_equity_ignores_the_operator_account(tmp_path):
    src, registry = _write_series(tmp_path, days=1)
    pack, sidecar = _build(tmp_path, src, registry)
    _rewrite(sidecar, share_scale=1.7)
    small = tmp_path / "small"
    large = tmp_path / "large"
    run_pack(BuyThenSell, pack, sidecar, "arena", small, initial_equity=1_000_000.0, with_benchmarks=False)
    run_pack(BuyThenSell, pack, sidecar, "arena", large, initial_equity=2_500_000.0, with_benchmarks=False)
    assert _tree(small / "designer") == _tree(large / "designer")
    left = json.loads((small / "metrics.json").read_text(encoding="utf-8"))
    right = json.loads((large / "metrics.json").read_text(encoding="utf-8"))
    assert left["initial_equity"] == 1_000_000.0
    assert right["initial_equity"] == 2_500_000.0
    assert left["final_equity"] != right["final_equity"]


@windows_jails(6)
def test_sidecar_ticker_does_not_change_designer_bytes(tmp_path):
    src, registry = _write_series(tmp_path, days=1)
    pack, sidecar = _build(tmp_path, src, registry, symbol="NIK", price_factor=1.7)
    _rewrite(sidecar, share_scale=1.7)
    base = tmp_path / "base"
    run_pack(BuyThenSell, pack, sidecar, "desk", base, with_benchmarks=False)
    base_tree = _tree(base / "designer")
    for symbol in ("COST", "FLAT"):
        _rewrite(sidecar, symbol=symbol)
        out = tmp_path / symbol
        payload = run_pack(BuyThenSell, pack, sidecar, "desk", out, with_benchmarks=False)
        assert payload["symbol"] == symbol
        assert _tree(out / "designer") == base_tree
        assert_designer_clean(out / "designer", symbol=symbol)


def test_edited_price_factor_is_rejected(tmp_path):
    src, registry = _write_series(tmp_path, days=1)
    pack, sidecar = _build(tmp_path, src, registry, symbol="NIK", price_factor=1.7)
    with pytest.raises(ValueError, match="factor_mac"):
        _rewrite(sidecar, allow_scale_mismatch=True, price_factor=3.3, share_scale=3.3)
    with pytest.raises(ValueError, match="factor_mac"):
        run_pack(BuyThenSell, pack, sidecar, "desk", tmp_path / "out", with_benchmarks=False)


def test_anonymized_sidecar_rejects_a_share_scale_other_than_the_price_factor(tmp_path):
    src, registry = _write_series(tmp_path, days=1)
    _pack, sidecar = _build(tmp_path, src, registry, symbol="NIK", price_factor=1.7)
    with pytest.raises(ValueError, match="factor_mac"):
        _rewrite(sidecar, share_scale=0.25)
    with pytest.raises(ValueError, match="must equal the build-time price factor"):
        _rewrite(sidecar, resign=True, share_scale=0.25)


@windows_jails(8)
def test_designer_dividend_is_real_div_times_build_price_factor(tmp_path):
    """A $1.25 dividend on a 1.7x pack pays 2.125 per pack share, at every billing scale."""
    src, registry = _write_series(tmp_path, days=2)
    pack, sidecar = _build(tmp_path, src, registry, price_factor=1.7)
    dividends = {"2024-03-05": 1.25}
    trees = []
    per_share = []
    for scale in SCALES:
        _rewrite(sidecar, allow_scale_mismatch=True, resign=True, share_scale=scale)
        out = tmp_path / f"div-{scale}"
        run_pack(
            BuyAndHold,
            pack,
            sidecar,
            "desk",
            out,
            dividends=dividends,
            with_benchmarks=False,
            allow_scale_mismatch=True,
        )
        designer = json.loads((out / "designer" / "metrics.json").read_text(encoding="utf-8"))
        curve = list(csv.DictReader((out / "designer" / "equity_curve.csv").open(encoding="utf-8")))
        assert "shares" not in curve[0]
        assert "dividends_net" not in designer
        comparison = json.loads((out / "cost_comparison.json").read_text(encoding="utf-8"))
        cash = comparison["books"]["strategy"]["designer_cost"]["dividends_net"]
        secret = load_sidecar(sidecar, allow_scale_mismatch=True)
        from backtester.runner import _designer_dividends

        mapped = _designer_dividends(dividends, secret["date_map"], secret["price_factor"])
        _result, metrics = designer_execute(
            BuyAndHold(), load_pack(pack).frame, "desk", dividends=mapped, with_benchmarks=False
        )
        shares = int(_result["daily"][0]["shares"])
        assert shares > 0
        assert cash == metrics["dividends_net"]
        assert cash / shares == pytest.approx(2.125)
        assert cash == pytest.approx(shares * 1.25 * 1.7)
        assert cash != pytest.approx(shares * 1.25)
        assert cash != pytest.approx(shares * 1.25 * scale) or scale == 1.7
        trees.append(_tree(out / "designer"))
        per_share.append(cash / shares)
        _assert_no_scale(_tree(out / "designer"))
    for tree in trees[1:]:
        assert tree == trees[0]
    assert per_share[0] == pytest.approx(2.125)


def test_cost_model_path_does_not_bind_share_scale():
    from test_engine import make_bars

    seen = []

    class Probe:
        def on_bar(self, _bar, _state):
            frame = sys._getframe()
            while frame is not None:
                if frame.f_code.co_name == "run" and frame.f_code.co_filename.endswith("engine_arena.py"):
                    seen.append("share_scale" in frame.f_locals)
                frame = frame.f_back
            return None

    engine_arena.run(Probe(), make_bars(days=1), cost_model=DesignerCostModel(0.8), share_scale=7.777)
    assert seen
    assert not any(seen)
    seen.clear()
    engine_arena.run(Probe(), make_bars(days=1), share_scale=1.7)
    assert any(seen)


def test_cost_model_path_drops_share_factor():
    seen = []

    class Probe:
        def on_bar(self, _row, _account):
            frame = sys._getframe()
            while frame is not None:
                code = frame.f_code
                if code.co_name == "run" and code.co_filename.endswith("engine_desk.py"):
                    cfg = frame.f_locals.get("cfg")
                    seen.append(getattr(cfg, "share_factor", "missing"))
                frame = frame.f_back
            return None

    bars = [
        _desk_bar("S1", "09:30", 100.0),
        _desk_bar("S1", "15:59", 100.0),
    ]
    designer = engine_desk.run(Probe(), bars, DeskConfig(cost_model=DesignerCostModel(0.8), share_factor=7.777))
    assert seen
    assert seen == ["missing"] * len(seen)
    assert "share_factor" not in designer["config"]
    assert "share_factor" not in engine_desk.metrics(designer)
    seen.clear()
    operator = engine_desk.run(Probe(), bars, DeskConfig(share_factor=1.7))
    assert 1.7 in seen
    assert operator["config"]["share_factor"] == 1.7
    assert engine_desk.metrics(operator)["share_factor"] == 1.7


def test_named_run_stays_on_the_real_schedule(tmp_path):
    src, _registry = _write_series(tmp_path, days=1)
    out = tmp_path / "named"
    payload = run_named(BuyThenSell, src, "NIK", "arena", out, with_benchmarks=False)
    assert payload["cost_basis"]["share_scale"] == 1.0
    assert payload["cost_basis"]["per_share_fees"] == "real_share_counts"
    assert "cost_model" not in payload
    assert not (out / "designer").exists()
    assert not (out / "cost_comparison.json").exists()
    frame = load_named(src, "NIK").frame
    _result, metrics = execute(BuyThenSell(), frame, "arena", share_factor=1.0)
    assert payload["fees_total"] == metrics["fees_total"]
    assert payload["final_equity"] == metrics["final_equity"]
    direct = engine_arena.run(BuyThenSell(), frame)
    assert "share_scale" in direct
    assert direct["fees"]["total"] == payload["fees_total"]


@windows_jails(4)
def test_walk_pack_charges_the_flat_fee_in_both_windows(tmp_path):
    src, registry = _write_series(tmp_path, days=4)
    pack, sidecar = _build(tmp_path, src, registry)
    _rewrite(sidecar, share_scale=1.7)
    out = tmp_path / "walk"
    summary = walk_pack(
        BuyThenSell,
        pack,
        sidecar,
        "arena",
        out,
        design_fraction=0.5,
        with_benchmarks=False,
        initial_equity=2_000_000.0,
    )
    assert summary["symbol"] == "NIK"
    assert not (out / "locked" / "designer").exists()
    designer = json.loads((out / "designer" / "metrics.json").read_text(encoding="utf-8"))
    assert designer["cost_model"] == "designer cost model: 0.8 bp flat"
    assert designer["initial_equity"] == DESIGNER_INITIAL_EQUITY
    assert designer["sessions"] == summary["design_sessions"]
    fills = list(csv.DictReader((out / "designer" / "fills.csv").open(encoding="utf-8")))
    assert fills
    assert {"shares_delta", "fill_price", "fees", "price"}.isdisjoint(fills[0])
    comparison = json.loads((out / "cost_comparison.json").read_text(encoding="utf-8"))
    assert set(comparison["windows"]) == {"design", "locked"}
    loaded = load_pack(pack)
    sliced = slice_walkforward(loaded.frame, design_fraction=0.5)
    _design_result, design_metrics = designer_execute(
        BuyThenSell(), sliced.design, "arena", with_benchmarks=False
    )
    _locked_result, locked_metrics = designer_execute(
        BuyThenSell(), sliced.locked_with_warmup, "arena", with_benchmarks=False
    )
    assert comparison["windows"]["design"]["books"]["strategy"]["designer_cost"]["fees_total"] == (
        design_metrics["fees_total"]
    )
    assert comparison["windows"]["locked"]["books"]["strategy"]["designer_cost"]["fees_total"] == (
        locked_metrics["fees_total"]
    )
    assert locked_metrics["fees_total"] > 0.0
    operator = json.loads((out / "design" / "metrics.json").read_text(encoding="utf-8"))
    assert operator["symbol"] == "NIK"
    assert operator["cost_basis"]["share_scale"] == 1.7
    assert operator["cost_basis"]["anonymized"] is True
    assert operator["cost_basis"]["per_share_fees"] == "real_share_counts"
    assert "price_factor" not in operator["cost_basis"]
    assert operator["initial_equity"] == 2_000_000.0
    assert "cost_model" not in operator
    locked_operator = json.loads((out / "locked" / "metrics.json").read_text(encoding="utf-8"))
    assert locked_operator["cost_basis"]["anonymized"] is True
    diff = comparison["windows"]["design"]["books"]["strategy"]["difference"]
    assert "final_equity" not in diff
    assert "final_equity" in comparison["windows"]["design"]["books"]["strategy"]["real_fees"]
    walk = json.loads((out / "walkforward.json").read_text(encoding="utf-8"))
    (out / "run.json").write_text(
        json.dumps({"schema_version": 1, "id": "rwalk", "kind": "walkforward", "profile": "arena"}) + "\n",
        encoding="utf-8",
    )
    designer_view = load_report(out, audience="designer")
    blob = json.dumps(designer_view)
    assert "summary" not in designer_view
    assert "cost_comparison" not in designer_view
    assert designer_view["metrics"]["total_return"] == designer["total_return"]
    assert "fees_total" not in designer_view["metrics"]
    assert designer_view["metrics"]["cost_drag_bps"] == designer["cost_drag_bps"]
    for number in (
        walk["design"]["total_return"],
        walk["locked"]["total_return"],
        walk["design"]["fees_total"],
        walk["locked"]["fees_total"],
    ):
        assert number != designer_view["metrics"]["total_return"]
        assert number != designer_view["metrics"]["cost_drag_bps"]
        assert json.dumps(number) not in blob
    from backtester.__main__ import _cmd_report

    designer_out = io.StringIO()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(sys, "stdout", designer_out)
        _cmd_report(SimpleNamespace(run=str(out), audience="designer"))
    printed = designer_out.getvalue()
    assert "locked total_return" not in printed
    assert "design total_return" not in printed
    for number in (walk["design"]["total_return"], walk["locked"]["total_return"], walk["design"]["fees_total"], walk["locked"]["fees_total"]):
        assert str(number) not in printed
    assert f"total_return={designer['total_return']}" in printed
    operator_view = load_report(out, audience="operator")
    assert operator_view["summary"]["locked"]["total_return"] == walk["locked"]["total_return"]
    assert "cost_comparison" in operator_view
    named = walk_forward(
        BuyThenSell,
        load_named(src, "NIK").frame,
        "arena",
        tmp_path / "named-walk",
        symbol="NIK",
        bar_minutes=1,
        design_fraction=0.5,
        with_benchmarks=False,
    )
    assert named[0]["symbol"] == "NIK"
    assert not (tmp_path / "named-walk" / "designer").exists()
    assert not (tmp_path / "named-walk" / "cost_comparison.json").exists()
    named_metrics = json.loads((tmp_path / "named-walk" / "design" / "metrics.json").read_text(encoding="utf-8"))
    assert named_metrics["cost_basis"]["anonymized"] is False


def test_model_slippage_matches_the_published_one_basis_point():
    from test_engine import make_bars

    model = DesignerCostModel(0.8)
    assert model.slipped_price(100.0, 1) == 100.0 * (1.0 + 1e-4)
    assert model.slipped_price(100.0, -1) == 100.0 * (1.0 - 1e-4)
    stressed = for_profile("desk", fee_mult=2.0)
    assert stressed.fill_costs(-50, 80.0)["total"] == _fee(-50, 80.0, bps=1.6)
    with pytest.raises(ValueError, match="fee-mult"):
        designer_execute(BuyThenSell(), make_bars(days=1), "arena", fee_mult=2.0)
