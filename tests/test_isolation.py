"""The library stays importable, path-free, and free of operator secrets."""

from __future__ import annotations

import argparse
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
    assert arena.borrow_rate_annual == 0.0030
    assert desk.borrow_rate_annual == 0.0025
    assert arena.borrow_rate_annual == arena_costs.BORROW_RATE_ANNUAL
    assert desk.borrow_rate_annual == costs_desk.BORROW_RATE_ANNUAL
    assert arena.borrow_rate_annual != desk.borrow_rate_annual
    assert arena.dividends is False and desk.dividends is True
    assert arena.liquidation_hwm_fraction is None
    assert desk.liquidation_hwm_fraction == 0.30
    assert arena.warmup != desk.warmup
    assert arena.fee_table != desk.fee_table
    assert engine_arena.C is arena_costs
    assert engine_desk.D is costs_desk
    assert engine_arena.C.SEC_FEE_RATE != engine_desk.D.SEC_FEE_RATE


def test_borrow_rate_help_names_both_profile_rates():
    from backtester.__main__ import _build_parser

    parser = _build_parser()
    sub = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
    text = sub.choices["run"].format_help()
    assert "0.0030" in text
    assert "0.0025" in text


def test_runner_charges_the_profile_borrow_rate():
    import pandas as pd

    from backtester.runner import execute

    rows = []
    for day, date in ((0, "2026-03-02"), (1, "2026-03-03")):
        for minute in (570, 571):
            rows.append(
                {
                    "date": date,
                    "day": day,
                    "weekday": 0,
                    "minute": minute,
                    "time": f"{minute // 60:02d}:{minute % 60:02d}",
                    "session": "rth",
                    "open": 100.0,
                    "high": 100.0,
                    "low": 100.0,
                    "close": 100.0,
                    "volume": 1000.0,
                    "wap": 100.0,
                    "count": 1,
                    "filled": 0,
                    "short_session": 0,
                }
            )
    frame = pd.DataFrame(rows)

    class Short:
        WANTS_EXTENDED_HOURS = False

        def on_bar(self, bar, _state):
            first = bar.get("day") == 0 or bar.get("session") == "2026-03-02"
            if first and (bar.get("minute") == 570 or bar.get("time") == "09:30"):
                return -1.0
            return None

    arena_result, arena_metrics = execute(Short(), frame, "arena")
    desk_result, desk_metrics = execute(Short(), frame, "desk")
    notional = 1_000_000.0
    assert arena_metrics["borrow_rate_annual"] == profiles.ARENA.borrow_rate_annual == 0.0030
    assert desk_metrics["borrow_rate_annual"] == profiles.DESK.borrow_rate_annual == 0.0025
    # Held across both session closes. Arena charges at each regular close.
    assert arena_result["borrow"] == 2 * (notional * 0.0030 / 252)
    # Desk charges once, at the next session boundary, on the previous close.
    assert desk_result["borrow"] == notional * 0.0025 / 252


def test_engines_do_not_import_each_other():
    desk_imports = _imports(ROOT / "src" / "backtester" / "engine_desk.py")
    arena_imports = _imports(ROOT / "src" / "backtester" / "engine_arena.py")
    cost_imports = _imports(ROOT / "src" / "backtester" / "costs_desk.py")
    assert not any("engine_arena" in name or name.endswith(".costs") for name in desk_imports)
    assert not any("engine_desk" in name or "costs_desk" in name for name in arena_imports)
    assert [name for name in cost_imports if name not in {"__future__", "math"}] == []


def _enclosing_function(tree: ast.AST) -> dict[int, str | None]:
    found: dict[int, str | None] = {}

    def visit(node: ast.AST, name: str | None) -> None:
        found[id(node)] = name
        for child in ast.iter_child_nodes(node):
            child_name = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else name
            visit(child, child_name)

    visit(tree, None)
    return found


def test_library_does_not_print_or_touch_the_working_directory():
    allowed = {"__main__.py", "arena_runner.py"}
    # These two restore the parent cwd before returning. The design-child test
    # checks that a walk-forward call leaves the caller in the directory it started in.
    cwd_funcs = {"_launch_book", "_enter_child"}
    for path in (ROOT / "src" / "backtester").glob("*.py"):
        if path.name in allowed:
            continue
        tree = _tree(path)
        owners = _enclosing_function(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"print", "input"}:
                raise AssertionError(f"{path.name} calls {node.func.id}")
            if isinstance(node, ast.Attribute) and node.attr in {"getcwd", "chdir"}:
                if owners.get(id(node)) not in cwd_funcs:
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
        # PyInstaller writes absolute paths into packaging/build and packaging/dist.
        rel_parts = path.relative_to(ROOT).parts
        if rel_parts[:2] in {("packaging", "build"), ("packaging", "dist")}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for token in SECRETS:
            assert token not in text, f"{path} contains {token}"
    # The desktop shell is the operator's process. Its preferences module is the
    # one place that names the default data folder. Designer packs and the
    # engines stay free of that path.
    operator_shell = ROOT / "src" / "backtester" / "gui" / "preferences.py"
    src_text = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in (ROOT / "src").rglob("*.py")
        if path != operator_shell
    )
    for token in USER_ROOT:
        assert token not in src_text
    assert r"C:\Users\Jeu\Desktop\Backtester" in operator_shell.read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    script = (ROOT / "run.ps1").read_text(encoding="utf-8")
    for blob in (readme, script):
        assert r"C:\Users\Jeu\Desktop\Backtester\1min_Candles-20261107T193139Z-1-001\1min_Candles" in blob
        assert r"C:\Users\Jeu\Desktop\Backtester\SPY\filled" in blob


def test_sample_fixtures_stay_under_a_megabyte():
    total = sum(path.stat().st_size for path in (ROOT / "assets").rglob("*") if path.is_file())
    assert total < 1_048_576
    assert total > 0


def test_a_ticker_is_not_a_code_fork():
    for name in ("data.py", "registry.py", "pack.py", "runner.py", "__main__.py"):
        text = (ROOT / "src" / "backtester" / name).read_text(encoding="utf-8")
        for token in ('== "QQQ"', "== 'QQQ'", '== "SPY"', "== 'SPY'"):
            assert token not in text, f"{name} branches on a ticker"


def test_import_is_side_effect_free(tmp_path):
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    code = (
        "import backtester, backtester.engine_arena, backtester.engine_desk, "
        "backtester.data, backtester.registry, backtester.pack, backtester.runner, "
        "backtester.costs, backtester.costs_desk, backtester.arena_runner"
    )
    subprocess.check_call([sys.executable, "-c", code], cwd=tmp_path, env=env)
    assert list(tmp_path.iterdir()) == []


def test_json_writer_rejects_non_finite():
    assert json_ready({"calmar": float("inf"), "gap": float("nan")}) == {"calmar": None, "gap": None}
