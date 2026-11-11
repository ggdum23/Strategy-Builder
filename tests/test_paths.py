"""Windows path construction, executed on whatever OS the suite is on."""

from pathlib import PureWindowsPath

import pytest

from backtester.data import monthly_filled_filename, monthly_filled_path

QQQ_ROOT = PureWindowsPath(
    r"C:\Users\Jeu\Desktop\Backtester\1min_Candles-20261107T193139Z-1-001\1min_Candles\QQQ\filled"
)
SPY_ROOT = PureWindowsPath(r"C:\Users\Jeu\Desktop\Backtester\SPY\filled")


def test_qqq_and_spy_roots_keep_windows_separators():
    qqq = monthly_filled_path(QQQ_ROOT, "QQQ", 2026, 10)
    spy = monthly_filled_path(SPY_ROOT, "SPY", 2026, 10)
    assert str(qqq) == (
        r"C:\Users\Jeu\Desktop\Backtester\1min_Candles-20261107T193139Z-1-001"
        r"\1min_Candles\QQQ\filled\QQQ_1min_filled_2026-10.csv.gz"
    )
    assert str(spy) == r"C:\Users\Jeu\Desktop\Backtester\SPY\filled\SPY_1min_filled_2026-10.csv.gz"
    assert isinstance(qqq, PureWindowsPath)
    assert isinstance(spy, PureWindowsPath)


def test_brk_b_and_rejects():
    path = monthly_filled_path(PureWindowsPath(r"C:\data\BRK.B\filled"), "BRK.B", 2024, 1)
    assert path.name == "BRK.B_1min_filled_2024-01.csv.gz"
    assert "\\" in str(path)
    with pytest.raises(ValueError):
        monthly_filled_filename("QQQ\\filled", 2024, 1)
    with pytest.raises(ValueError):
        monthly_filled_path(QQQ_ROOT, "QQQ", 2026, 13)
