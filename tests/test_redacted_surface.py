"""Seventh review. Scale literals and paths share one check. No Qt required."""

from __future__ import annotations

import pytest

from backtester.gui.audience import designer_surface_text, export_allowed, normalize_surface, redacted_key_allowed

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
    (2, "20", True),
    (0.25, "25", True),
    (0.5, "50", True),
    (0.7, "07", True),
    (0.25, "25e0.5", False),
    (0.123, "123e0.5", True),
    (7.777, "7777e-3.5", True),
    (1e-7, "build2_1e\u22127", True),
    (7.777, "\uff17\uff0e\uff17\uff17\uff17", True),
)


@pytest.mark.parametrize(("scale", "token", "dropped"), _SCALE_CASES)
def test_scale_literals_match_value_significand_reciprocal_and_powers(scale, token, dropped):
    shown = designer_surface_text(f"see {token} now", (), scale=scale)
    folded = normalize_surface(token)
    if dropped:
        assert token not in shown
        assert folded not in shown
        assert folded not in shown.replace(" ", "")
        assert not export_allowed(token, symbols=(), scale=scale)
        assert not export_allowed(f"see {token} now", symbols=(), scale=scale)
    else:
        assert token in shown
        # A decimal of at least 1 is a free-text price even when it is not the scale.
        priced = token in {"2.5", "build2_7.777"}
        assert export_allowed(token, symbols=(), scale=scale) is not priced
        assert export_allowed(f"see {token} now", symbols=(), scale=scale) is not priced


def test_long_2x_and_2_5_stay_together_at_scale_2():
    assert designer_surface_text("long_2x and 2.5", (), scale=2) == "long_2x and 2.5"
    assert not export_allowed("long_2x and 2.5", symbols=(), scale=2)
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
    assert redacted_key_allowed("cost_drag_bps")
    assert not redacted_key_allowed("fees_total")
    assert not redacted_key_allowed("total")
    assert redacted_key_allowed("B01")
    assert redacted_key_allowed("S01")
    assert redacted_key_allowed("M1")
    assert redacted_key_allowed("Y2")
    assert not redacted_key_allowed("S99")
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


def test_allow_listed_numbers_are_not_scale_scanned():
    """Ordinary Desk figures share significand digits with common invoice scales.

    The number under an allow-listed key stays. The same digits in free text
    are still a leak.
    """
    import json

    payload = {
        "audience": "designer",
        "borrow_rate_annual": 0.0025,
        "cash_rate_annual": 0.05,
        "financing_rate_annual": 0.07,
        "total_return": 0.2,
        "n_fills": 2,
        "accrual": "session_boundary_1_252",
        "initial_equity": 1_000_000,
        "cost_drag_bps": 1.2,
    }
    text = json.dumps(payload)
    for scale in (0.25, 0.5, 0.7, 2):
        assert export_allowed(text, symbols=(), scale=scale), scale
    assert not export_allowed("borrow 0.0025", symbols=(), scale=0.25)
    assert not export_allowed("cash 0.05", symbols=(), scale=0.5)
    assert not export_allowed("financing 0.07", symbols=(), scale=0.7)
    assert not export_allowed("return 0.2", symbols=(), scale=2)
    leaked = dict(payload, message="build2_7777e-3")
    assert not export_allowed(json.dumps(leaked), symbols=(), scale=7.777)


_CLOCKS = ("09:20", "09:25", "09:50", "07:30", "09:31", "09:25:00")
_CLOCK_SCALES = (None, 0.25, 0.5, 0.7, 2, 7.777)


def test_clock_times_stay_whole_and_are_not_the_scale():
    """``09:25`` is one token. The minute is not a one- or two-digit scale."""
    for stamp in _CLOCKS:
        for scale in _CLOCK_SCALES:
            shown = designer_surface_text(f"fill {stamp} rth", (), scale=scale)
            assert shown == f"fill {stamp} rth", (stamp, scale, shown)
            assert export_allowed(f"fill {stamp} rth", symbols=(), scale=scale)
    row = (
        "session,time,phase,minute,kind\n"
        ",09:25,rth,565,strategy\n"
        ",09:20,pre,560,strategy\n"
        ",09:50,rth,590,strategy\n"
        ",07:30,pre,450,strategy\n"
    )
    for scale in (0.25, 0.5, 0.7, 2):
        assert export_allowed(row, symbols=(), scale=scale), scale


def test_real_equity_png_is_text_only_and_a_planted_chunk_is_rejected(tmp_path):
    """A matplotlib equity figure passes. A tEXt chunk that names the scale or the ticker does not."""
    import struct
    import zlib

    from backtester.gui.audience import export_text, scan_publication
    from backtester.report import assert_designer_clean, write_book

    book = tmp_path / "book"
    result = {
        "daily": [{"session": "S00001", "ordinal": 0, "equity": 1_000_000.0, "exposure": 0.0}],
        "fills": [],
        "episodes": [],
    }
    write_book(book, "desk", result, {"total_return": 0.0, "profile": "desk"}, None)
    png = book / "equity.png"
    blob = png.read_bytes()
    assert blob.startswith(b"\x89PNG\r\n\x1a\n")
    text = export_text(png)
    assert "IDAT" not in text
    assert "equity" in text
    assert len(text) < 200
    for scale in (None, 0.25, 0.5, 0.7, 2, 7.777):
        assert export_allowed(text, symbols=("QQQ",), scale=scale), scale
        assert scan_publication(text, symbols=("QQQ",), scale=scale) is None
    assert_designer_clean(book, symbol="QQQ", scale=7.777)

    def plant(keyword: str, caption: str) -> bytes:
        data = keyword.encode("latin-1") + b"\x00" + caption.encode("latin-1")
        crc = zlib.crc32(b"tEXt" + data) & 0xFFFFFFFF
        chunk = struct.pack(">I", len(data)) + b"tEXt" + data + struct.pack(">I", crc)
        start = blob.rfind(b"IEND") - 4
        return blob[:start] + chunk + blob[start:]

    scaled = tmp_path / "scaled.png"
    scaled.write_bytes(plant("Comment", "scale 7.777"))
    assert "7.777" in export_text(scaled)
    assert not export_allowed(export_text(scaled), symbols=(), scale=7.777)
    named = tmp_path / "named.png"
    named.write_bytes(plant("Comment", "held QQQ"))
    assert not export_allowed(export_text(named), symbols=("QQQ",), scale=None)
    fake = tmp_path / "notes.png"
    fake.write_bytes(b"not a png scale 7.777")
    assert "7.777" in export_text(fake)
    assert not export_allowed(export_text(fake), symbols=(), scale=7.777)
    png.write_bytes(plant("Comment", "scale 7.777"))
    with pytest.raises(RuntimeError):
        assert_designer_clean(book, symbol="QQQ", scale=7.777)


_PLANT = "fill 450.12 scale 7.777 and \u22127.777 and \uff17\uff0e\uff17\uff17\uff17"
_NEEDLES = ("450.12", "7.777", "\u22127.777", "\uff17\uff0e\uff17\uff17\uff17", "build_7.777")


def test_a_pack_id_is_not_a_price_and_the_plant_still_is():
    """Hex in ``pack_<sha>`` is not a price and not the scale. The review's plant is."""
    import json

    from backtester.gui.audience import scan_publication

    text = json.dumps(
        {"source_id": "pack_c3e04dc938e1", "name": "pack_92057aa05783", "strategy_id": "flat"}
    )
    for scale in (None, 1.15, 1.35, 7.777):
        assert scan_publication(text, scale=scale) is None
    planted = json.dumps({"message": _PLANT, "name": "build_7.777"}, ensure_ascii=False)
    assert scan_publication(planted, scale=7.777) == "the scale"
    assert scan_publication(json.dumps({"note": "close 450.12"}), scale=None) == "a price"


def test_publish_strips_free_text_and_aborts_when_it_survives(tmp_path, monkeypatch):
    """Allow-listed keys are typed. A price left in a string fails the publish and removes the tree."""
    import json

    from backtester.designer_allow import project_designer
    from backtester.report import assert_designer_clean, publish_designer

    metrics = {
        "profile": "desk",
        "audience": "designer",
        "strategy_id": "flat",
        "name": "build_7.777",
        "total_return": 0.2,
        "initial_equity": 1_000_000,
        "final_equity": 1_000_000,
        "accrual": "session_boundary_1_252",
        "price_basis": "raw",
        "share_factor": 7.777,
        "cost_basis": {"share_scale": 7.777, "share_factor": 7.777},
        "message": _PLANT,
        "M1": "close 450.12",
        "error": "the strategy failed",
    }
    cleaned = project_designer(metrics)
    blob = json.dumps(cleaned, ensure_ascii=False)
    for needle in _NEEDLES:
        assert needle not in blob
    assert cleaned["total_return"] == 0.2
    assert cleaned["error"] == "the strategy failed"
    assert cleaned["strategy_id"] == "flat"
    assert cleaned["accrual"] == "session_boundary_1_252"
    assert "message" not in cleaned
    assert "M1" not in cleaned
    assert "name" not in cleaned

    result = {
        "daily": [{"session": "S00001", "ordinal": 0, "equity": 1_000_000.5, "exposure": 0.0}],
        "fills": [
            {"session": "", "time": "09:25", "phase": "rth", "minute": 565, "kind": "strategy"},
            {"session": "S00001", "time": "09:20", "phase": "pre", "minute": 560, "kind": "strategy"},
            {"session": "S00001", "time": "09:50", "phase": "rth", "minute": 590, "kind": "strategy"},
            {"session": "S00001", "time": "07:30", "phase": "pre", "minute": 450, "kind": "strategy"},
        ],
        "episodes": [],
    }
    from backtester.report import write_book

    out = tmp_path / "book"
    write_book(out, "desk", result, metrics, None)
    operator_lines = (out / "equity_curve.csv").read_text(encoding="utf-8").splitlines()
    operator_header = operator_lines[0].split(",")
    operator_equity = [
        line.split(",")[operator_header.index("equity")] for line in operator_lines[1:] if line
    ]
    publish_designer(out, "desk", None, {"strategy": (result, metrics)}, metrics, symbol="QQQ")
    designer = out / "designer"
    text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in designer.rglob("*")
        if path.is_file() and path.suffix != ".png"
    )
    for needle in _NEEDLES:
        assert needle not in text
    fills = (designer / "fills.csv").read_text(encoding="utf-8")
    for stamp in ("09:20", "09:25", "09:50", "07:30"):
        assert stamp in fills
    designer_lines = (designer / "equity_curve.csv").read_text(encoding="utf-8").splitlines()
    designer_header = designer_lines[0].split(",")
    designer_equity = [
        line.split(",")[designer_header.index("equity")] for line in designer_lines[1:] if line
    ]
    assert designer_equity == operator_equity
    assert designer_equity == ["1000000.5"]

    dirty = tmp_path / "dirty"
    dirty.mkdir()
    (dirty / "metrics.json").write_text(
        json.dumps({"message": _PLANT, "M1": "close 450.12", "name": "build_7.777"}, ensure_ascii=False),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="scale|price"):
        assert_designer_clean(dirty, symbol="QQQ", scale=7.777)

    import backtester.report as report

    monkeypatch.setattr(report, "project_designer", lambda value: value)
    leaked = tmp_path / "leaked"
    with pytest.raises(RuntimeError):
        publish_designer(leaked, "desk", None, {"strategy": (result, metrics)}, metrics, symbol="QQQ")
    assert not (leaked / "designer").exists()
