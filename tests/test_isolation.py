"""The library stays importable, path-free, and free of operator secrets."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

from backtester import costs as arena_costs
from backtester import costs_desk, engine_arena, engine_desk, profiles
from backtester.report import json_ready

ROOT = Path(__file__).resolve().parents[1]

SECRETS = ("/workspace", "/home/", "secrets.json", "arena_qqq", "ibpull")
USER_ROOT = ("C:\\Users\\Jeu", "C:/Users/Jeu")


def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"))


def _imports(path: Path) -> list[str]:
    names = []
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def test_profiles_do_not_share_a_venue_or_a_fee_table():
    arena = profiles.ARENA
    desk = profiles.DESK
    assert arena.fill_venue != desk.fill_venue
    assert arena.max_abs_target != desk.max_abs_target
    assert arena.financing_rate_annual == 0.0
    assert desk.financing_rate_annual == 0.07
    assert arena.borrow_rate_annual != desk.borrow_rate_annual
    assert arena.dividends is False and desk.dividends is True
    assert arena.liquidation_hwm_fraction is None
    assert desk.liquidation_hwm_fraction == 0.30
    assert arena.warmup != desk.warmup
    assert arena.fee_table != desk.fee_table
    assert engine_arena.C is arena_costs
    assert engine_desk.D is costs_desk
    assert engine_arena.C.SEC_FEE_RATE != engine_desk.D.SEC_FEE_RATE


def test_engines_do_not_import_each_other():
    desk_imports = _imports(ROOT / "src" / "backtester" / "engine_desk.py")
    arena_imports = _imports(ROOT / "src" / "backtester" / "engine_arena.py")
    cost_imports = _imports(ROOT / "src" / "backtester" / "costs_desk.py")
    assert not any("engine_arena" in name or name.endswith(".costs") for name in desk_imports)
    assert not any("engine_desk" in name or "costs_desk" in name for name in arena_imports)
    assert [name for name in cost_imports if name not in {"__future__", "math"}] == []


def test_library_does_not_print_or_touch_the_working_directory():
    allowed = {"__main__.py", "arena_runner.py"}
    for path in (ROOT / "src" / "backtester").glob("*.py"):
        if path.name in allowed:
            continue
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"print", "input"}:
                raise AssertionError(f"{path.name} calls {node.func.id}")
            if isinstance(node, ast.Attribute) and node.attr in {"getcwd", "chdir"}:
                raise AssertionError(f"{path.name} uses {node.attr}")


def test_no_secret_paths():
    roots = [
        ROOT / "src",
        ROOT / "examples",
        ROOT / "docs",
        ROOT / "packaging",
        ROOT / "README.md",
        ROOT / "run.ps1",
        ROOT / "run.bat",
    ]
    files = []
    for root in roots:
        if root.is_file():
            files.append(root)
        else:
            files.extend(path for path in root.rglob("*") if path.is_file())
    for path in files:
        if path.suffix in {".gz", ".png", ".pyc"} or "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for token in SECRETS:
            assert token not in text, f"{path} contains {token}"
    src_text = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in (ROOT / "src").rglob("*.py")
    )
    for token in USER_ROOT:
        assert token not in src_text
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    script = (ROOT / "run.ps1").read_text(encoding="utf-8")
    for blob in (readme, script):
        assert r"C:\Users\Jeu\Desktop\Backtester\1min_Candles-20261107T193139Z-1-001\1min_Candles" in blob
        assert r"C:\Users\Jeu\Desktop\Backtester\SPY\filled" in blob


def test_sample_fixtures_stay_under_a_megabyte():
    total = sum(path.stat().st_size for path in (ROOT / "assets").rglob("*") if path.is_file())
    assert total < 1_048_576
    assert total > 0


def test_import_is_side_effect_free(tmp_path):
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    code = (
        "import backtester, backtester.engine_arena, backtester.engine_desk, "
        "backtester.data, backtester.runner, backtester.costs, backtester.costs_desk, "
        "backtester.arena_runner"
    )
    subprocess.check_call([sys.executable, "-c", code], cwd=tmp_path, env=env)
    assert list(tmp_path.iterdir()) == []


def test_json_writer_rejects_non_finite():
    assert json_ready({"calmar": float("inf"), "gap": float("nan")}) == {"calmar": None, "gap": None}
