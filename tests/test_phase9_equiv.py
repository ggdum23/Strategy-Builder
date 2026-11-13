"""Byte-identity with main 2b73448 on a synthetic 20-session series.

The golden file is the normalized report tree of that commit, captured on
this host and checked against a second run. ``sidecar.json`` drops
``factor_key``, ``factor_mac``, and ``source_root`` (a random key and the
operator's directory). ``run.json`` drops ``id``. PNG bytes are locked on
Linux, where they matched main; Windows checks the PNG signature because
the chart font is not the same file.

Packs refuse ``--unsafe-in-process``. The jailed pack is the pack surface.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from backtester.pack import build_pack
from backtester.parallel import NamedMarket, discover_strategies, run_parallel
from backtester.runner import run_named, run_pack, walk_forward, walk_pack
from backtester.synthetic import write_series
from windows_open import windows_jails

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = json.loads((ROOT / "tests" / "golden_main_2b73448.json").read_text(encoding="utf-8"))
_PNG = b"\x89PNG\r\n\x1a\n"
_HOLD = (
    "class Strategy:\n"
    "    def on_bar(self, bar, state):\n"
    "        phase = bar.get('phase', bar.get('session'))\n"
    "        if phase == 'rth' and bar.get('time') == '09:30':\n"
    "            return 1.0\n"
    "        return None\n"
)
_FLAT = (
    "class Strategy:\n"
    "    def on_bar(self, bar, state):\n"
    "        phase = bar.get('phase', bar.get('session'))\n"
    "        return None\n"
)


class _Hold:
    def on_bar(self, bar, _state):
        phase = bar.get("phase", bar.get("session"))
        if phase == "rth" and bar.get("time") == "09:30":
            return 1.0
        return None


def _hash(path: Path) -> str:
    name = path.name
    if name == "sidecar.json":
        document = json.loads(path.read_text(encoding="utf-8"))
        for key in ("factor_key", "factor_mac", "source_root"):
            document.pop(key, None)
        blob = json.dumps(document, indent=2, sort_keys=True).encode()
    elif name == "run.json":
        document = json.loads(path.read_text(encoding="utf-8"))
        document.pop("id", None)
        blob = json.dumps(document, indent=2, sort_keys=True).encode()
    else:
        blob = path.read_bytes()
    return hashlib.sha256(blob).hexdigest()


def _compare(folder: Path, case: str) -> None:
    expected = dict(GOLDEN["cases"][case])
    actual = {}
    for path in sorted(folder.rglob("*")):
        if not path.is_file():
            continue
        rel = str(path.relative_to(folder))
        if path.suffix.lower() == ".png" and sys.platform == "win32":
            assert path.read_bytes().startswith(_PNG), rel
            expected.pop(rel, None)
            continue
        actual[rel] = _hash(path)
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    changed = sorted(key for key in set(expected) & set(actual) if expected[key] != actual[key])
    assert not missing and not extra and not changed, (case, missing[:5], extra[:5], changed[:8])


def _series(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "src"
    write_series(source, "SYN", sessions=int(GOLDEN["sessions"]))
    strategy = tmp_path / "hold.py"
    strategy.write_text(_HOLD, encoding="utf-8")
    return source, strategy


def _strategies(tmp_path: Path) -> Path:
    root = tmp_path / "strategies"
    for name, body in (("s0", _HOLD), ("s1", _FLAT)):
        folder = root / name
        folder.mkdir(parents=True)
        (folder / "strategy.py").write_text(body, encoding="utf-8")
    return root


def _cache(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("STRATEGY_BUILDER_PREPARED_DIR", str(tmp_path / "prepared"))


def test_inprocess_arena_and_desk_match_main(tmp_path, monkeypatch):
    _cache(monkeypatch, tmp_path)
    source, strategy = _series(tmp_path)
    run_named(strategy, source, "SYN", "arena", tmp_path / "arena", in_process=True)
    run_named(strategy, source, "SYN", "desk", tmp_path / "desk", in_process=True)
    _compare(tmp_path / "arena", "arena-inproc")
    _compare(tmp_path / "desk", "desk-inproc")


def test_inprocess_walk_forward_matches_main(tmp_path, monkeypatch):
    from backtester.data import load_named

    _cache(monkeypatch, tmp_path)
    source, _strategy = _series(tmp_path)
    frame = load_named(source, "SYN").frame
    for profile in ("arena", "desk"):
        walk_forward(
            _Hold,
            frame,
            profile,
            tmp_path / profile,
            symbol="SYN",
            bar_minutes=1,
            design_fraction=0.5,
            in_process=True,
        )
        _compare(tmp_path / profile, f"walk-{profile}-inproc")


@windows_jails(2)
def test_jailed_arena_and_desk_match_main(tmp_path, monkeypatch):
    _cache(monkeypatch, tmp_path)
    source, strategy = _series(tmp_path)
    run_named(strategy, source, "SYN", "arena", tmp_path / "arena", in_process=False)
    run_named(strategy, source, "SYN", "desk", tmp_path / "desk", in_process=False)
    _compare(tmp_path / "arena", "arena-jailed")
    _compare(tmp_path / "desk", "desk-jailed")


@windows_jails(4)
def test_jailed_walk_forward_matches_main(tmp_path, monkeypatch):
    from backtester.data import load_named

    _cache(monkeypatch, tmp_path)
    source, strategy = _series(tmp_path)
    frame = load_named(source, "SYN").frame
    for profile in ("arena", "desk"):
        walk_forward(
            strategy,
            frame,
            profile,
            tmp_path / profile,
            symbol="SYN",
            bar_minutes=1,
            design_fraction=0.5,
            in_process=False,
        )
        _compare(tmp_path / profile, f"walk-{profile}")


@windows_jails(6)
def test_pack_runs_match_main(tmp_path, monkeypatch):
    _cache(monkeypatch, tmp_path)
    source, strategy = _series(tmp_path)
    for profile in ("arena", "desk"):
        dest = tmp_path / profile
        pack = dest / "pack"
        side = dest / "sidecar.json"
        build_pack(source, "SYN", pack, side, anonymize=False, operator_note="equiv")
        run_pack(strategy, pack, side, profile, dest / "run", in_process=False)
        _compare(dest, f"pack-{profile}-jailed")
    for profile in ("arena", "desk"):
        dest = tmp_path / f"walk-{profile}"
        pack = dest / "pack"
        side = dest / "sidecar.json"
        build_pack(source, "SYN", pack, side, anonymize=False, operator_note="equiv")
        walk_pack(strategy, pack, side, profile, dest / "walk", design_fraction=0.5)
        _compare(dest, f"walk-pack-{profile}")


def test_pack_refuses_in_process(tmp_path):
    source, strategy = _series(tmp_path)
    pack = tmp_path / "pack"
    side = tmp_path / "sidecar.json"
    build_pack(source, "SYN", pack, side, anonymize=False, operator_note="equiv")
    with pytest.raises(ValueError, match="unsafe-in-process cannot run a pack"):
        run_pack(strategy, pack, side, "arena", tmp_path / "out", in_process=True)


@windows_jails(6)
def test_parallel_matches_main(tmp_path, monkeypatch):
    _cache(monkeypatch, tmp_path)
    source, _strategy = _series(tmp_path)
    discovered = discover_strategies(_strategies(tmp_path))
    market = NamedMarket(str(source), "SYN")
    run_parallel(discovered, [market], tmp_path / "arena", profile="arena", workers=2, with_benchmarks=True)
    run_parallel(discovered, [market], tmp_path / "desk", profile="desk", workers=2, with_benchmarks=True)
    run_parallel(discovered, [market], tmp_path / "one", profile="arena", workers=1, with_benchmarks=False)
    _compare(tmp_path / "arena", "parallel-arena")
    _compare(tmp_path / "desk", "parallel-desk")
    _compare(tmp_path / "one", "parallel-arena-1")


def test_cold_warm_and_uncached_reports_match(tmp_path, monkeypatch):
    """The prepared cache does not change a book. A failed cache is the same book."""
    cache = tmp_path / "prepared"
    cache.mkdir()
    monkeypatch.setenv("STRATEGY_BUILDER_PREPARED_DIR", str(cache))
    source, strategy = _series(tmp_path)
    cold = tmp_path / "cold"
    run_named(strategy, source, "SYN", "arena", cold, in_process=True)
    assert any(cache.iterdir())
    warm = tmp_path / "warm"
    run_named(strategy, source, "SYN", "arena", warm, in_process=True)
    _compare(cold, "arena-inproc")
    _compare(warm, "arena-inproc")

    def _refuse(*_args, **_kwargs):
        raise RuntimeError("cache disabled")

    monkeypatch.setattr("backtester.prepared.lookup_named", _refuse)
    monkeypatch.setattr("backtester.prepared.save_named", _refuse)
    plain = tmp_path / "plain"
    run_named(strategy, source, "SYN", "arena", plain, in_process=True)
    _compare(plain, "arena-inproc")


@windows_jails(3)
def test_jailed_cache_paths_match_main(tmp_path, monkeypatch):
    cache = tmp_path / "prepared"
    cache.mkdir()
    monkeypatch.setenv("STRATEGY_BUILDER_PREPARED_DIR", str(cache))
    source, strategy = _series(tmp_path)
    cold = tmp_path / "cold"
    run_named(strategy, source, "SYN", "arena", cold, in_process=False)
    warm = tmp_path / "warm"
    run_named(strategy, source, "SYN", "arena", warm, in_process=False)

    def _refuse(*_args, **_kwargs):
        raise RuntimeError("cache disabled")

    monkeypatch.setattr("backtester.prepared.lookup_named", _refuse)
    monkeypatch.setattr("backtester.prepared.save_named", _refuse)
    plain = tmp_path / "plain"
    run_named(strategy, source, "SYN", "arena", plain, in_process=False)
    _compare(cold, "arena-jailed")
    _compare(warm, "arena-jailed")
    _compare(plain, "arena-jailed")
