"""Seventh review. Scale literals and paths share one check. No Qt required."""

from __future__ import annotations

import pytest

from backtester.gui.audience import designer_surface_text, export_allowed, redacted_key_allowed

# (scale, token, dropped)
_SCALE_CASES = (
    (7.777, "build2_7777e-3", True),
    (7.777, "7.777e-3", True),
    (7.777, "7.777e3", True),
    (7.777, "build2_7.777e3", True),
    (7.777, "build2_7.777", True),
    (7.777, "v2_7.777", True),
    (7.777, "scale__7.777", True),
    (7.777, "build2_1e7.777", True),
    (1e-7, "build2_1e-7", True),
    (1e-4, "scale1e-4", True),
    (1e7, "v2_1e+7", True),
    (2, "long_2x", False),
    (2, "2.5", False),
    (2, "12", False),
    (2, "build2_7.777", False),
    (2, "2", True),
    (2, "2.0", True),
)


@pytest.mark.parametrize(("scale", "token", "dropped"), _SCALE_CASES)
def test_scale_literals_match_value_significand_reciprocal_and_powers(scale, token, dropped):
    shown = designer_surface_text(f"see {token} now", (), scale=scale)
    if dropped:
        assert token not in shown
        assert token not in shown.replace(" ", "")
        assert not export_allowed(token, symbols=(), scale=scale)
        assert not export_allowed(f"see {token} now", symbols=(), scale=scale)
    else:
        assert token in shown
        assert export_allowed(token, symbols=(), scale=scale)
        assert export_allowed(f"see {token} now", symbols=(), scale=scale)


def test_long_2x_and_2_5_stay_together_at_scale_2():
    assert designer_surface_text("long_2x and 2.5", (), scale=2) == "long_2x and 2.5"
    assert export_allowed("long_2x and 2.5", symbols=(), scale=2)
    assert not export_allowed("held 2 shares", symbols=(), scale=2)
    assert not export_allowed("scale 2", symbols=(), scale=2)


_PATHS = (
    r"C:Users\Jeu\secret.txt",
    r"C:secret\NOTES.md",
    r"hidden\file.txt",
    r"dir\secret\notes.txt",
    r"graded\out.csv",
    r"foo\NOTES.md",
    r"foo\hidden\bar, still there",
    r"Jeu \\?\C:\data.csv",
    r"'\\?\C:\data.csv' Jeu",
)


@pytest.mark.parametrize("sample", _PATHS)
def test_drive_relative_and_secret_paths_use_the_shared_check(sample):
    shown = designer_surface_text(f"open {sample} please", ()).casefold()
    for needle in ("jeu", "users", "secret", "hidden", "graded", "notes.md", "data.csv"):
        assert needle not in shown, (sample, needle, shown)
    assert not export_allowed(sample, symbols=(), scale=None)


def test_a_bare_secret_word_and_a_harmless_backslash_stay():
    assert "secret" in designer_surface_text("the secret is out", ())
    assert export_allowed("the secret is out", symbols=(), scale=None)
    kept = designer_surface_text(r"see foo\bar now", ())
    assert "foo" in kept and "bar" in kept
    assert export_allowed(r"see foo\bar now", symbols=(), scale=None)
    noted = designer_surface_text("note C: the book", ())
    assert "note" in noted and "book" in noted


def test_new_metric_keys_are_hidden_unless_listed():
    assert redacted_key_allowed("total_return")
    assert redacted_key_allowed("fees_total")
    assert redacted_key_allowed("total")
    assert redacted_key_allowed("B01")
    assert redacted_key_allowed("S01")
    assert redacted_key_allowed("M1")
    assert not redacted_key_allowed("final_shares")
    assert not redacted_key_allowed("shares_before")
    assert not redacted_key_allowed("exchange")
    assert not redacted_key_allowed("sec")
    assert not redacted_key_allowed("counted_shares")
    assert not redacted_key_allowed("tier_shares")
    assert not redacted_key_allowed("surprise_price")
    assert not redacted_key_allowed("close")
    assert not redacted_key_allowed("2024")
    # Settings cannot be inverted into a price or a share count.
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
        assert redacted_key_allowed(key), key
    # Dollars and a per-share dividend amount can be inverted.
    for key in ("cash_interest", "financing_cost", "dividends_net", "dividend_per_share", "amount"):
        assert not redacted_key_allowed(key), key
