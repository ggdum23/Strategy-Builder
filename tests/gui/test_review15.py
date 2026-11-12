"""Fifteenth review. A banner is the exception sentence, read off the exception."""

from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTEST_QT_API", "pyside6")

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from PySide6.QtWidgets import QApplication

from backtester.gui.jobs import JobFailure, build_pack_job, process_entry
from backtester.gui.workers import FunctionThread
from synthetic import write_filled, write_strategy
from test_review5 import _banners
from test_window import _window

_MAC = "sidecar factor_mac: sidecar has no operator factor MAC; rebuild the pack"
_RANDOM = (
    "Symbol 'RANDOM' collides with a reserved report name and cannot be anonymized; "
    "run it as an operator (raw) book or rename the symbol in the data."
)
_GENERIC = "unknown mode nope"


def _show(window, failure) -> None:
    window.run_page.on_failed(failure)
    window.packs_page._failed(failure)
    QApplication.processEvents()


def _assert_exact(window, sentence: str, source: str) -> None:
    run = _banners(window.run_page)
    packs = _banners(window.packs_page)
    assert run == sentence
    assert packs == sentence
    assert window.packs_page.summary.text() == sentence
    log = window.run_page.log.toPlainText()
    assert "Traceback" in log
    assert source in log
    assert "Traceback" not in run
    assert source not in run
    assert not run.startswith("ValueError:")


def _from_status(path) -> JobFailure:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["state"] == "error"
    failure = JobFailure.from_dict(payload["failure"])
    assert failure is not None
    assert failure.type_name == "ValueError"
    return failure


def test_macless_sidecar_banners_the_exception_on_run_and_packs(qtbot, tmp_path):
    src = write_filled(tmp_path / "filled")
    registry = tmp_path / "registry.json"
    from backtester.gui.jobs import record_series

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
    document = json.loads(sidecar.read_text(encoding="utf-8"))
    document.pop("factor_mac", None)
    document.pop("factor_key", None)
    sidecar.write_text(json.dumps(document), encoding="utf-8")
    strategy = write_strategy(tmp_path / "strategy.py", 1.0)
    status = tmp_path / "status.json"
    config = {
        "mode": "single",
        "profile": "arena",
        "source": "pack",
        "strategies": [str(strategy)],
        "pack": str(pack),
        "sidecar": str(sidecar),
        "out": str(tmp_path / "out"),
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
    process_entry("single", config, str(status))
    failure = _from_status(status)
    assert failure.message == _MAC
    window = _window(qtbot, tmp_path)
    _show(window, failure)
    _assert_exact(window, _MAC, "_fail(")


def test_random_symbol_banners_the_exception_on_run_and_packs(qtbot, tmp_path):
    caught: list = []
    thread = FunctionThread(
        lambda: build_pack_job(
            tmp_path / "missing",
            "RANDOM",
            tmp_path / "pack",
            tmp_path / "sidecar.json",
            tmp_path / "registry.json",
            anonymize=True,
        )
    )
    thread.failed.connect(caught.append)
    thread.run()
    assert len(caught) == 1
    failure = caught[0]
    assert isinstance(failure, JobFailure)
    assert failure.type_name == "ValueError"
    assert failure.message == _RANDOM
    window = _window(qtbot, tmp_path)
    _show(window, failure)
    _assert_exact(window, _RANDOM, "require_anonymizable_symbol")


def test_unknown_mode_banners_the_exception_on_run_and_packs(qtbot, tmp_path):
    status = tmp_path / "status.json"
    process_entry("nope", {"mode": "nope"}, str(status))
    failure = _from_status(status)
    assert failure.message == _GENERIC
    window = _window(qtbot, tmp_path)
    _show(window, failure)
    _assert_exact(window, _GENERIC, "unknown mode {mode}")
