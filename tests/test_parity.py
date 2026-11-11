"""Bit-identity of the ported Arena engine against the vendored reference."""

from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path

import pytest

from backtester import costs as port_costs
from backtester import engine_arena as port
from backtester.data import load_named
from backtester.strategy_loader import load_strategy_class

ROOT = Path(__file__).resolve().parents[1]
REF = ROOT / "tests" / "reference" / "arena"

RESULT_KEYS = (
    "initial_equity",
    "daily",
    "fills",
    "episodes",
    "open_episode",
    "fees",
    "slippage",
    "borrow",
    "traded_notional",
    "eth_notional",
    "n_fills",
    "busted",
    "bar_exposure",
    "wants_extended_hours",
)


def _vendor_hashes():
    mapping = {
        "backtest.py": REF / "backtest.py",
        "costs.py": REF / "costs.py",
        "run_backtest.py": REF / "run_backtest.py",
        "example_strategy.py": REF / "example_strategy.py",
        "tests/test_engine.py": REF / "ref_test_engine.py",
        "data/MANIFEST.json": REF / "MANIFEST.json",
    }
    for line in (REF / "ENGINE_SHA256.txt").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, name = line.split(maxsplit=1)
        if name in mapping:
            assert hashlib.sha256(mapping[name].read_bytes()).hexdigest() == digest
        else:
            assert not (ROOT / name).exists()
    assert (ROOT / "src" / "backtester" / "costs.py").read_bytes() == (REF / "costs.py").read_bytes()


def load_reference():
    sys.path.insert(0, str(REF))
    try:
        for name in ("backtest", "costs"):
            sys.modules.pop(name, None)
        import backtest as ref_backtest
        import costs as ref_costs
    finally:
        try:
            sys.path.remove(str(REF))
        except ValueError:
            pass
    assert ref_backtest.C is ref_costs
    assert ref_costs is not port_costs
    assert ref_costs.TAF_PER_SHARE == 0.000195
    return ref_backtest


def _canon(value) -> str:
    """Sorted-key serialization. Floats use repr of the Python float."""
    if isinstance(value, dict):
        body = ",".join(f"{key!r}:{_canon(value[key])}" for key in sorted(value, key=str))
        return "{" + body + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_canon(item) for item in value) + "]"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            return _canon(value.item())
        except Exception:
            pass
    if isinstance(value, float):
        return repr(float(value))
    if isinstance(value, int):
        return str(int(value))
    return repr(value)


def _same(ref_result, port_result, ref_mod, label):
    for key in RESULT_KEYS:
        left = ref_result[key]
        right = port_result[key]
        if left != right:
            raise AssertionError(f"{label} {key} diverged:\n{left!r}\n!=\n{right!r}")
    left_m = ref_mod.metrics(ref_result)
    right_m = port.metrics(port_result)
    if left_m != right_m:
        mismatched = [key for key in left_m if left_m[key] != right_m.get(key)]
        raise AssertionError(f"{label} metrics diverged on {mismatched}")
    left_bytes = _canon(left_m).encode("utf-8")
    right_bytes = _canon(right_m).encode("utf-8")
    if left_bytes != right_bytes:
        raise AssertionError(f"{label} metric bytes diverged")


class ShortOne:
    """None of the twenty designer strategies go short. This one does."""

    WANTS_EXTENDED_HOURS = False

    def on_bar(self, _bar, _state):
        return -1.0


def _cases():
    cases = []
    for number in range(1, 21):
        path = ROOT / "examples" / "strategies" / f"D{number:02d}" / "strategy.py"
        cases.append((f"D{number:02d}", load_strategy_class(path)))
    base = load_strategy_class(ROOT / "examples" / "strategies" / "D02" / "strategy.py")

    class D02Extended(base):
        WANTS_EXTENDED_HOURS = True

    cases.append(("D02-extended", D02Extended))
    cases.append(("short-1x", ShortOne))
    return cases


@pytest.fixture(scope="module")
def qqq():
    return load_named(ROOT / "assets" / "sample" / "QQQ" / "filled", "QQQ").frame


def test_vendor_bytes_and_sample_parity(qqq):
    _vendor_hashes()
    ref = load_reference()
    # Official borrow rate through the override must match the default call.
    short_bars = qqq.iloc[:4000].copy()
    default = port.run(ShortOne(), short_bars)
    overridden = port.run(ShortOne(), short_bars, borrow_rate_annual=port_costs.BORROW_RATE_ANNUAL)
    assert default["borrow"] == overridden["borrow"]
    assert default["borrow"] > 0.0
    zero = port.run(ShortOne(), short_bars, borrow_rate_annual=0.0)
    assert zero["borrow"] == 0.0
    assert zero["daily"] != default["daily"]

    table = ["strategy,final_equity,n_fills,fees,borrow,eth_notional,extended,short"]
    extended = {"D02-extended"}
    short = {"short-1x"}
    for label, cls in _cases():
        ref_result = ref.run(cls(), qqq)
        port_result = port.run(cls(), qqq)
        _same(ref_result, port_result, ref, label)
        # The reference class and the port class are separate objects. Same behavior.
        if label == "D01":
            ref_bh = ref.run(ref.BuyAndHold(), qqq)
            port_bh = port.run(port.BuyAndHold(), qqq)
            _same(ref_bh, port_bh, ref, "buy-and-hold")
        metrics = port.metrics(port_result)
        table.append(
            ",".join(
                [
                    label,
                    repr(metrics["final_equity"]),
                    str(metrics["n_fills"]),
                    repr(metrics["fees_total"]),
                    repr(metrics["borrow_cost"]),
                    repr(port_result["eth_notional"]),
                    "yes" if label in extended else "no",
                    "yes" if label in short else "no",
                ]
            )
        )
    # Levered originals are in the twenty. Extended hours and the short are added
    # because none of those files set either flag.
    assert any(row.startswith("D02,") or row.startswith("D14,") or row.startswith("D20,") for row in table)
    text = "\n".join(table) + "\n"
    # Review artifact. tempfile.gettempdir() is the platform temp directory,
    # so the same write works on Windows and Linux.
    out = Path(tempfile.gettempdir()) / "strategy_builder_parity_table.csv"
    out.write_text(text, encoding="utf-8")
    assert out.read_text(encoding="utf-8") == text
    assert "D02-extended" in text
    assert "short-1x" in text
