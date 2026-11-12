"""Sixteenth review. A strategy exception banners one line. The traceback stays in the log."""

from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTEST_QT_API", "pyside6")

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from PySide6.QtWidgets import QApplication

from backtester.gui.jobs import JobFailure, build_pack_job, process_entry, record_series
from backtester.gui.workers import FunctionThread
from backtester.runner import failure_line
from synthetic import write_filled
from windows_open import windows_jails
from test_review5 import _banners, _table_text
from test_window import _window

_CASES = (
    (
        "value",
        "class Strategy:\n"
        "    def on_bar(self, bar, state):\n"
        "        raise ValueError('book rejected\\nthe bar')\n",
        "ValueError: book rejected the bar",
        ("book rejected", "the bar"),
        (),
    ),
    (
        "source",
        "class Strategy:\n"
        "    def on_bar(self, bar, state):\n"
        "        raise RuntimeError('require_anonymizable_symbol(symbol)')\n",
        "RuntimeError: require_anonymizable_symbol(symbol)",
        ("require_anonymizable_symbol(symbol)", "in on_bar", "raise RuntimeError"),
        ("def on_bar",),
    ),
    (
        "empty",
        "class Strategy:\n"
        "    def on_bar(self, bar, state):\n"
        "        raise OSError()\n",
        "strategy raised OSError",
        ("OSError",),
        ("run failed",),
    ),
    (
        "chain",
        "class Strategy:\n"
        "    def on_bar(self, bar, state):\n"
        "        try:\n"
        "            raise RuntimeError('inner cause')\n"
        "        except RuntimeError as exc:\n"
        "            raise ValueError('outer failure') from exc\n",
        "ValueError: outer failure",
        ("inner cause", "direct cause"),
        ("inner cause",),
    ),
    (
        "interrupt",
        "class Strategy:\n"
        "    def on_bar(self, bar, state):\n"
        "        raise KeyboardInterrupt()\n",
        "strategy raised KeyboardInterrupt",
        ("KeyboardInterrupt",),
        ("run failed",),
    ),
    (
        "halt",
        "class Halt(BaseException):\n"
        "    pass\n"
        "\n"
        "class Strategy:\n"
        "    def on_bar(self, bar, state):\n"
        "        raise Halt('stop the book')\n",
        "Halt: stop the book",
        ("Halt",),
        (),
    ),
)


def _anon_pack(tmp_path):
    src = write_filled(tmp_path / "filled")
    registry = tmp_path / "registry.json"
    record_series(src, "QQQ", registry, profile="arena", price_kind="raw", bar_minutes=1)
    pack = tmp_path / "pack"
    sidecar = tmp_path / "operator" / "sidecar.json"
    build_pack_job(
        src,
        "QQQ",
        pack,
        sidecar,
        registry,
        anonymize=True,
        profile="arena",
        bar_minutes=1,
        price_kind="raw",
    )
    return pack, sidecar


def _config(strategy, pack, sidecar, out) -> dict:
    return {
        "mode": "single",
        "profile": "arena",
        "source": "pack",
        "strategies": [str(strategy)],
        "pack": str(pack),
        "sidecar": str(sidecar),
        "out": str(out),
        "bar_minutes": 1,
        "initial_equity": 1_000_000,
        "borrow_rate_annual": None,
        "financing_rate_annual": None,
        "fee_mult": 1,
        "dividends": "",
        "warmup_sessions": 0,
        "warmup_days": 0,
        "design_end_day": None,
        "design_end_date": "",
        "design_fraction": None,
    }


def _failure(status) -> JobFailure:
    payload = json.loads(status.read_text(encoding="utf-8"))
    assert payload["state"] == "error", payload.get("state")
    failure = JobFailure.from_dict(payload["failure"])
    assert failure is not None
    assert failure.strategy is True
    assert failure.type_name != "WorkerFailure"
    return failure


@windows_jails(14)
def test_on_bar_exceptions_banner_one_line_on_run_and_packs(qtbot, tmp_path):
    pack, sidecar = _anon_pack(tmp_path)
    window = _window(qtbot, tmp_path)
    for name, source, banner, in_log, absent in _CASES:
        strategy = tmp_path / f"{name}.py"
        strategy.write_text(source, encoding="utf-8")
        status = tmp_path / f"{name}-status.json"
        process_entry("single", _config(strategy, pack, sidecar, tmp_path / name), str(status))
        failure = _failure(status)
        assert failure_line(failure.type_name, failure.message) == banner
        window.run_page.on_failed(failure)
        window.packs_page._failed(failure)
        QApplication.processEvents()
        run = _banners(window.run_page)
        packs = _banners(window.packs_page)
        assert run == banner, name
        assert packs == banner, name
        assert window.packs_page.summary.text() == banner
        assert "\n" not in run
        assert len(run) <= 240
        assert "Traceback" not in run
        assert "File " not in run
        for word in absent:
            assert word not in run, (name, word)
        log = window.run_page.log.toPlainText()
        assert "Traceback" in log
        for word in in_log:
            assert word in log, (name, word)
        assert run not in {"", "run failed"}

    long_text = "X" * 400
    strategy = tmp_path / "long.py"
    strategy.write_text(
        "class Strategy:\n"
        "    def on_bar(self, bar, state):\n"
        f"        raise ValueError({long_text!r})\n",
        encoding="utf-8",
    )
    status = tmp_path / "long-status.json"
    process_entry("single", _config(strategy, pack, sidecar, tmp_path / "long"), str(status))
    failure = _failure(status)
    window.run_page.on_failed(failure)
    window.packs_page._failed(failure)
    QApplication.processEvents()
    banner = _banners(window.run_page)
    assert banner == failure_line("ValueError", long_text)
    assert banner.endswith("...")
    assert len(banner) <= 240
    assert "\n" not in banner
    assert banner == _banners(window.packs_page)
    assert window.packs_page.summary.text() == banner
    assert "Traceback" in window.run_page.log.toPlainText()
    assert "Traceback" not in banner


def test_function_thread_records_base_exception(qtbot, tmp_path):
    class Halt(BaseException):
        pass

    caught: list = []

    def boom():
        raise Halt("stop")

    thread = FunctionThread(boom)
    thread.failed.connect(caught.append)
    thread.run()
    assert len(caught) == 1
    assert isinstance(caught[0], JobFailure)
    assert caught[0].type_name == "Halt"
    assert caught[0].message == "stop"

    caught.clear()

    def interrupt():
        raise KeyboardInterrupt()

    thread = FunctionThread(interrupt)
    thread.failed.connect(caught.append)
    thread.run()
    assert len(caught) == 1
    assert caught[0].type_name == "KeyboardInterrupt"

    caught.clear()

    def leave():
        raise SystemExit(3)

    thread = FunctionThread(leave)
    thread.failed.connect(caught.append)
    thread.run()
    assert len(caught) == 1
    assert caught[0].type_name == "SystemExit"


def test_leaderboard_error_without_error_json_reaches_the_jobs_table(qtbot, tmp_path):
    root = tmp_path / "batch"
    job = root / "jobs" / "theta" / "QQQ"
    job.mkdir(parents=True)
    (job / "metrics.json").write_text("{}\n", encoding="utf-8")
    (root / "leaderboard.csv").write_text(
        "strategy_id,source_id,status,error\n"
        "theta,QQQ,error,RuntimeError: the bar was rejected\n",
        encoding="utf-8",
    )
    (root / "run.json").write_text(
        json.dumps(
            {
                "kind": "parallel",
                "jobs": [{"strategy_id": "theta", "source_id": "QQQ", "status": "error"}],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    designer = root / "designer" / "theta" / "QQQ"
    designer.mkdir(parents=True)
    (designer / "metrics.json").write_text("{}\n", encoding="utf-8")
    (root / "designer" / "leaderboard.csv").write_text(
        "strategy_id,source_id,status,error\n"
        "theta,QQQ,error,the strategy failed\n",
        encoding="utf-8",
    )
    window = _window(qtbot, tmp_path)
    page = window.results_page
    page.load_path(str(root), audience="operator")
    QApplication.processEvents()
    table = _table_text(page.jobs)
    banners = _banners(page)
    assert "RuntimeError: the bar was rejected" in table
    assert "RuntimeError: the bar was rejected" in banners
    page.load_path(str(root), audience="designer")
    QApplication.processEvents()
    shown = _banners(page) + "\n" + _table_text(page.jobs)
    assert "the strategy failed" in shown
    assert "rejected" not in shown.casefold()
    assert "runtimeerror" not in shown.casefold()
