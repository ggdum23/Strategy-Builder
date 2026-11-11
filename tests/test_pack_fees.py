"""Raw and anonymized runs bill the same per-share fees. The child never sees the sidecar."""

from __future__ import annotations

import gzip
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from backtester import costs as arena_costs
from backtester import costs_desk as desk_costs
from backtester.data import load_named
from backtester.pack import assert_anonymized_pack_clean, build_pack, load_pack
from backtester.registry import entry_from_inspection, save_registry, upsert_series
from backtester.runner import execute, run_named, run_pack

ROOT = Path(__file__).resolve().parents[1]
QQQ = ROOT / "assets" / "sample" / "QQQ" / "filled"
K = 1.7
VOLUME = 0.42
ET = timezone(timedelta(hours=-4))


class RoundTrip:
    """Buy on the first regular session, flat from the eighth. No clock is hardcoded.

    The same class runs on Arena bars (phase is ``session``) and Desk bars
    (phase is ``phase``). Session identity is the day index or the session id.
    """

    WANTS_EXTENDED_HOURS = False

    def __init__(self):
        self.seen = []

    def on_bar(self, bar, _state):
        phase = bar["phase"] if "phase" in bar else bar["session"]
        if phase != "rth":
            return None
        key = bar["session"] if "phase" in bar else bar["day"]
        if key in self.seen:
            return None
        self.seen.append(key)
        n = len(self.seen)
        if n == 1:
            return 1.0
        if n >= 8:
            return 0.0
        return None


class SidecarHunter:
    """Looks for the operator canary on the channels Phase 1 actually isolates."""

    WANTS_EXTENDED_HOURS = False

    def __init__(self):
        self.saw = None
        self.hit = ""
        self.argv = None
        self.cwd = ""
        self.files = []
        self.env_keys = []

    def on_bar(self, _bar, _state):
        if self.saw is not None:
            return 0.0
        import gc
        import os as os_mod
        import re as re_mod
        import sys

        prefix = "SB-" + "CANARY-"
        pattern = re_mod.compile(re_mod.escape(prefix) + r"[0-9a-f]{32}")
        parts = ["\n".join(sys.argv)]
        parts.append("\n".join(f"{key}={value}" for key, value in os_mod.environ.items()))
        cwd = os_mod.getcwd()
        parts.append(cwd)
        names = []
        for dirpath, _dirs, files in os_mod.walk(cwd):
            for name in files:
                names.append(name)
                try:
                    with open(os_mod.path.join(dirpath, name), "rb") as handle:
                        parts.append(handle.read().decode("utf-8", "replace"))
                except OSError:
                    continue
        self.argv = list(sys.argv)
        self.cwd = cwd
        self.files = names
        self.env_keys = sorted(os_mod.environ)
        token = prefix.encode()
        for obj in list(gc.get_objects()):
            try:
                if isinstance(obj, str) and prefix in obj:
                    parts.append(obj)
                elif isinstance(obj, (bytes, bytearray)) and token in obj:
                    parts.append(bytes(obj).decode("utf-8", "replace"))
            except Exception:
                continue
        match = pattern.search("\n".join(parts))
        self.saw = match is not None
        self.hit = "" if match is None else match.group(0)
        return 0.0


def _registry(tmp_path):
    loaded = load_named(QQQ, "QQQ")
    entry = entry_from_inspection(loaded.inspection, source_root=str(QQQ), profile="arena")
    path = tmp_path / "registry.json"
    save_registry(path, upsert_series({"schema_version": 1, "series": []}, entry))
    return path


def _sale(frame: pd.DataFrame) -> float:
    total = 0.0
    for delta, price in zip(frame["shares_delta"], frame["fill_price"]):
        if float(delta) < 0.0:
            total += abs(float(delta)) * float(price)
    return total


def _position_error(raw_deltas, pack_deltas, k: float) -> tuple[float, float]:
    residual = 0.0
    error = 0.0
    raw_pos = 0.0
    pack_pos = 0.0
    for raw, pack in zip(raw_deltas, pack_deltas):
        residual += abs(abs(float(raw)) - abs(float(pack)) * k)
        raw_pos += float(raw)
        pack_pos += float(pack)
        error = max(error, abs(raw_pos - pack_pos * k))
    return residual, error


@pytest.fixture(scope="module")
def anonymized_qqq(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("qqq-pack")
    registry = _registry(tmp)
    pack = tmp / "pack"
    sidecar = tmp / "operator" / "qqq.json"
    build_pack(
        QQQ,
        "QQQ",
        pack,
        sidecar,
        anonymize=True,
        registry_path=registry,
        profile="arena",
        price_factor=K,
        volume_factor=VOLUME,
    )
    return pack, sidecar


@pytest.mark.parametrize("profile", ["arena", "desk"])
def test_anonymized_pack_matches_raw_per_share_fees(tmp_path, anonymized_qqq, profile):
    pack, sidecar = anonymized_qqq
    assert_anonymized_pack_clean(pack, symbol="QQQ")
    schedule = arena_costs if profile == "arena" else desk_costs
    raw_dir = tmp_path / "raw"
    pack_dir = tmp_path / "pack-run"
    raw = run_named(RoundTrip, QQQ, "QQQ", profile, raw_dir, with_benchmarks=False)
    packed = run_pack(RoundTrip, pack, sidecar, profile, pack_dir, with_benchmarks=False)
    assert "strategy_observer" not in (pack_dir / "metrics.json").read_text(encoding="utf-8")
    file_metrics = json.loads((pack_dir / "metrics.json").read_text(encoding="utf-8"))
    assert file_metrics["cost_basis"]["share_scale"] == K
    assert file_metrics["cost_basis"]["share_factor"] == K
    assert file_metrics["cost_basis"]["anonymized"] is True
    assert file_metrics["cost_basis"]["per_share_fees"] == "real_share_counts"
    assert file_metrics["cost_basis"]["value_fees"] == "unscaled_trade_value"
    assert file_metrics["cost_basis"]["cost_schedule"] == profile
    assert file_metrics["cost_basis"]["profile"] == profile
    assert "share_scale" in file_metrics["cost_basis"]["note"]
    assert "volume_factor" not in (pack_dir / "metrics.json").read_text(encoding="utf-8")
    assert "date_map" not in (pack_dir / "metrics.json").read_text(encoding="utf-8")
    assert raw["cost_basis"]["share_scale"] == 1.0
    assert raw["cost_basis"]["anonymized"] is False
    assert raw["cost_basis"]["cost_schedule"] == profile
    borrow = 0.003 if profile == "arena" else 0.0025
    assert packed["borrow_rate_annual"] == borrow
    assert raw["borrow_rate_annual"] == borrow
    raw_fills = pd.read_csv(raw_dir / "fills.csv")
    pack_fills = pd.read_csv(pack_dir / "fills.csv")
    assert len(raw_fills) == len(pack_fills) == 2
    assert (raw_fills["shares_delta"] < 0).any()
    assert (pack_fills["shares_delta"] < 0).any()
    residual, error = _position_error(raw_fills["shares_delta"], pack_fills["shares_delta"], K)
    assert error < 5.0
    rates = {
        "commission": schedule.COMMISSION_TIERS[0][1],
        "exchange": schedule.EXCHANGE_PER_SHARE,
        "clearing": schedule.CLEARING_PER_SHARE,
        "cat": schedule.CAT_PER_SHARE,
        "taf": schedule.TAF_PER_SHARE,
    }
    raw_fees = raw["fees_breakdown"]
    pack_fees = packed["fees_breakdown"]
    assert raw_fees["taf"] > 0.0 and pack_fees["taf"] > 0.0
    for key, rate in rates.items():
        assert abs(raw_fees[key] - pack_fees[key]) <= residual * rate + 1e-6
    pass_rate = rates["commission"] * (schedule.PASS_THROUGH_NYSE + schedule.PASS_THROUGH_FINRA)
    assert abs(raw_fees["pass_through"] - pack_fees["pass_through"]) <= residual * pass_rate + 1e-6
    sale_gap = abs(_sale(raw_fills) - _sale(pack_fills))
    assert abs(raw_fees["sec"] - pack_fees["sec"]) <= sale_gap * schedule.SEC_FEE_RATE + 1e-6
    loaded = load_named(QQQ, "QQQ")
    price_range = float(loaded.frame["high"].max() - loaded.frame["low"].min())
    gap = abs(raw["final_equity"] - packed["final_equity"])
    assert gap <= error * price_range + abs(raw_fees["total"] - pack_fees["total"]) + 1.0
    _wrong_result, wrong = execute(RoundTrip(), load_pack(pack).frame, profile, share_factor=1.0)
    assert abs(raw_fees["commission"] - wrong["fees_breakdown"]["commission"]) > 1.0
    assert abs(pack_fees["sec"] - wrong["fees_breakdown"]["sec"]) <= 1e-6
    curve = (pack_dir / "equity_curve.csv").read_text(encoding="utf-8")
    assert "2026-" not in curve


class TargetOnce:
    """One target on the first regular bar. Later bars leave the position alone."""

    WANTS_EXTENDED_HOURS = False
    TARGET = 1.0

    def __init__(self):
        self.sent = False

    def on_bar(self, bar, _state):
        phase = bar["phase"] if "phase" in bar else bar["session"]
        if phase != "rth" or self.sent:
            return None
        self.sent = True
        return self.TARGET


class TargetShort(TargetOnce):
    TARGET = -1.0


class ScaleFrame:
    """Records runner-frame locals while on_bar is on the stack."""

    WANTS_EXTENDED_HOURS = False
    SCALE = 1.7

    def __init__(self):
        self.scale = self.SCALE
        self.runner = []
        self.engine_scale = None
        self.desk_scale = None

    def on_bar(self, _bar, _state):
        if self.runner:
            return 0.0
        import sys

        frame = sys._getframe()
        while frame is not None:
            code = frame.f_code
            locs = frame.f_locals
            if code.co_name in {"execute", "run_books", "_books_worker"} and code.co_filename.endswith("runner.py"):
                bad = {}
                for key, value in locs.items():
                    if key in {"share_scale", "share_factor"} or _holds_scale(value, self.scale):
                        bad[key] = True
                self.runner.append((code.co_name, bad))
            if code.co_name == "run" and code.co_filename.endswith("engine_arena.py"):
                self.engine_scale = locs.get("share_scale")
            if code.co_name == "run" and code.co_filename.endswith("engine_desk.py"):
                cfg = locs.get("cfg")
                self.desk_scale = None if cfg is None else getattr(cfg, "share_factor", None)
            frame = frame.f_back
        return 0.0


def _holds_scale(value, scale):
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return value == scale
    if isinstance(value, dict):
        if "share_scale" in value or "share_factor" in value:
            return True
        return any(_holds_scale(item, scale) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_holds_scale(item, scale) for item in value)
    share = getattr(value, "share_factor", None)
    return isinstance(share, (int, float)) and not isinstance(share, bool) and share == scale


def _ohlc(day: datetime, minute: int) -> dict:
    stamp = day.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=minute)
    stamp = stamp.replace(tzinfo=ET)
    return {
        "timestamp_et": stamp.isoformat(),
        "epoch_utc": int(stamp.timestamp()),
        "open": 100.0,
        "high": 100.5,
        "low": 99.5,
        "close": 100.0,
        "volume": 1000,
        "wap": 100.0,
        "count": 4,
        "filled": 0,
        "session": "rth",
    }


def _dividend_series(tmp_path):
    day1 = datetime(2024, 3, 4)
    day2 = datetime(2024, 3, 5)
    rows = [
        _ohlc(day1, 9 * 60 + 30),
        _ohlc(day1, 9 * 60 + 31),
        _ohlc(day2, 9 * 60 + 30),
    ]
    path = tmp_path / "src" / "NIK_1min_filled_2024-03.csv.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    header = [
        "timestamp_et", "epoch_utc", "open", "high", "low", "close", "volume", "wap", "count", "filled", "session",
    ]
    lines = [",".join(header)]
    for row in rows:
        lines.append(",".join(str(row[name]) for name in header))
    with gzip.open(path, "wt", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")
    loaded = load_named(path.parent, "NIK")
    entry = entry_from_inspection(loaded.inspection, source_root=str(path.parent), profile="desk")
    registry = tmp_path / "registry.json"
    save_registry(registry, upsert_series({"schema_version": 1, "series": []}, entry))
    pack = tmp_path / "pack"
    sidecar = tmp_path / "side.json"
    build_pack(
        path.parent, "NIK", pack, sidecar,
        anonymize=True, registry_path=registry, profile="desk",
        price_factor=2.5, volume_factor=0.4,
    )
    return path.parent, pack, sidecar


@pytest.mark.parametrize("strategy_cls,cash", [(TargetOnce, 12500.0), (TargetShort, -12500.0)])
def test_anonymized_desk_dividends_match_raw_cash(tmp_path, strategy_cls, cash, monkeypatch):
    src, pack, sidecar = _dividend_series(tmp_path)
    dividends = {"2024-03-05": 1.25}
    seen = {}
    import backtester.runner as runner_mod

    real = runner_mod._isolated_books

    def wrapped(strategy_cls, frame, profile, *, during=None, **kwargs):
        seen["dividends"] = kwargs.get("dividends")
        return real(strategy_cls, frame, profile, during=during, **kwargs)

    monkeypatch.setattr(runner_mod, "_isolated_books", wrapped)
    raw = run_named(
        strategy_cls, src, "NIK", "desk", tmp_path / "raw",
        dividends=dividends, with_benchmarks=False,
    )
    packed = run_pack(
        strategy_cls, pack, sidecar, "desk", tmp_path / "packed",
        dividends=dividends, with_benchmarks=False,
    )
    assert raw["dividends_net"] == cash
    assert packed["dividends_net"] == cash
    assert seen["dividends"] == {"S00002": 1.25 * 2.5}
    assert "2024-03-05" not in seen["dividends"]
    assert 1.25 not in seen["dividends"].values()


def test_an_unknown_dividend_date_is_an_error(tmp_path):
    _src, pack, sidecar = _dividend_series(tmp_path)
    with pytest.raises(ValueError, match="1999-01-01"):
        run_pack(
            TargetOnce, pack, sidecar, "desk", tmp_path / "out",
            dividends={"1999-01-01": 1.25}, with_benchmarks=False,
        )


def test_child_runner_frames_do_not_keep_the_scale(tmp_path):
    src, registry = _tiny_series(tmp_path)
    pack = tmp_path / "pack"
    sidecar = tmp_path / "side.json"
    build_pack(
        src, "NIK", pack, sidecar,
        anonymize=True, registry_path=registry, profile="desk",
        price_factor=ScaleFrame.SCALE, volume_factor=0.4,
    )
    for profile, attr in (("arena", "engine_scale"), ("desk", "desk_scale")):
        payload = run_pack(
            ScaleFrame, pack, sidecar, profile, tmp_path / profile, with_benchmarks=False,
        )
        observer = payload["strategy_observer"]
        names = [name for name, _bad in observer.runner]
        assert {"execute", "run_books", "_books_worker"} <= set(names)
        assert all(not bad for _name, bad in observer.runner)
        assert getattr(observer, attr) == ScaleFrame.SCALE


def test_the_report_must_not_live_inside_the_pack(tmp_path):
    pack = tmp_path / "pack"
    pack.mkdir()
    message = "the report must not live inside the pack directory"
    with pytest.raises(ValueError, match=message):
        run_pack(RoundTrip, pack, tmp_path / "side.json", "arena", pack / "out")
    link = tmp_path / "link"
    try:
        link.symlink_to(pack, target_is_directory=True)
    except OSError as exc:
        pytest.fail(f"symlink coverage did not run: {exc}")
    with pytest.raises(ValueError, match=message):
        run_pack(RoundTrip, pack, tmp_path / "side.json", "arena", link / "nested")
    with pytest.raises(ValueError, match=message):
        run_pack(RoundTrip, link, tmp_path / "side.json", "arena", pack)


def _tiny_series(tmp_path):
    day = datetime(2024, 3, 4)
    rows = []
    for offset, minute in enumerate((8 * 60, 8 * 60 + 1)):
        stamp = (day + timedelta(minutes=minute)).replace(tzinfo=ET)
        price = 15.0 + offset
        rows.append(
            {
                "timestamp_et": stamp.isoformat(),
                "epoch_utc": int(stamp.timestamp()),
                "open": price,
                "high": price + 0.4,
                "low": price - 0.4,
                "close": price + 0.1,
                "volume": 50,
                "wap": price,
                "count": 2,
                "filled": 0,
                "session": "rth",
            }
        )
    path = tmp_path / "src" / "NIK_1min_filled_2024-03.csv.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    header = [
        "timestamp_et", "epoch_utc", "open", "high", "low", "close", "volume", "wap", "count", "filled", "session",
    ]
    lines = [",".join(header)]
    for row in rows:
        lines.append(",".join(str(row[name]) for name in header))
    with gzip.open(path, "wt", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")
    loaded = load_named(path.parent, "NIK")
    entry = entry_from_inspection(loaded.inspection, source_root=str(path.parent), profile="desk")
    registry = tmp_path / "registry.json"
    save_registry(registry, upsert_series({"schema_version": 1, "series": []}, entry))
    return path.parent, registry


def test_explicit_borrow_overrides_the_run_profile(tmp_path):
    src, _registry = _tiny_series(tmp_path)
    loaded = load_named(src, "NIK")
    entry = entry_from_inspection(
        loaded.inspection, source_root=str(src), profile="arena", borrow_rate_annual=0.01
    )
    registry = tmp_path / "explicit-registry.json"
    save_registry(registry, upsert_series({"schema_version": 1, "series": []}, entry))
    pack = tmp_path / "explicit-pack"
    sidecar = tmp_path / "explicit-sidecar.json"
    build_pack(
        src, "NIK", pack, sidecar,
        anonymize=True, registry_path=registry, profile="arena",
        price_factor=0.5, volume_factor=0.4,
    )
    secret = json.loads(sidecar.read_text(encoding="utf-8"))
    assert secret["borrow_rate_source"] == "explicit"
    assert secret["borrow_rate_annual"] == 0.01
    for profile in ("arena", "desk"):
        payload = run_pack(
            RoundTrip, pack, sidecar, profile, tmp_path / profile, with_benchmarks=False,
        )
        assert payload["borrow_rate_annual"] == 0.01
        assert payload["cost_basis"]["profile"] == profile


def test_strategy_process_cannot_see_the_sidecar(tmp_path, monkeypatch):
    src, registry = _tiny_series(tmp_path)
    canary = "SB-" + "CANARY-" + secrets.token_hex(16)
    assert re.fullmatch(r"SB-CANARY-[0-9a-f]{32}", canary)
    assert canary not in Path(__file__).read_text(encoding="utf-8")
    pack = tmp_path / "pack"
    sidecar = tmp_path / "operator" / "note.json"
    built = build_pack(
        src,
        "NIK",
        pack,
        sidecar,
        anonymize=True,
        registry_path=registry,
        profile="desk",
        price_factor=0.25,
        volume_factor=0.3,
        operator_note=canary,
    )
    assert canary in sidecar.read_text(encoding="utf-8")
    assert_anonymized_pack_clean(pack, symbol="NIK")
    import backtester.runner as runner_mod

    seen = {}
    real = runner_mod._isolated_books

    def wrapped(strategy_cls, frame, profile, *, during=None, **kwargs):
        seen["kwargs"] = dict(kwargs)
        seen["columns"] = list(frame.columns)
        seen["text"] = frame.to_csv(index=False)
        return real(strategy_cls, frame, profile, during=during, **kwargs)

    monkeypatch.setattr(runner_mod, "_isolated_books", wrapped)
    os.environ["SB_PACK_SECRET"] = canary
    try:
        payload = run_pack(
            SidecarHunter,
            pack,
            sidecar,
            "arena",
            tmp_path / "out",
            with_benchmarks=False,
        )
    finally:
        os.environ.pop("SB_PACK_SECRET", None)
    assert seen["kwargs"]["share_factor"] == built.share_scale == 0.25
    assert "date_map" not in seen["kwargs"]
    assert "operator_note" not in seen["kwargs"]
    assert "symbol" not in seen["kwargs"]
    assert canary not in seen["text"]
    assert "NIK" not in seen["text"]
    assert "symbol" not in seen["columns"]
    observer = payload["strategy_observer"]
    assert observer.saw is False, observer.hit
    assert observer.argv == ["backtester-design"]
    assert observer.inherited_argv == ["backtester-design"]
    assert "SB_PACK_SECRET" not in observer.env_keys
    assert canary not in observer.cwd
    assert canary not in observer.inherited_cwd
    assert observer.files == []
    assert canary not in (tmp_path / "out" / "equity_curve.csv").read_text(encoding="utf-8")
