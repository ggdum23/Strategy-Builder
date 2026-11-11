"""Write metrics.json, the equity curve, the fill and trade files, and the PNG.

An anonymized run also gets a ``designer/`` tree. That tree is what a strategy
designer may be shown. The files beside it are the operator copy.
"""

from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import shutil
import struct
import sys
import tempfile
import threading
import zlib
from pathlib import Path

import pandas as pd

_ISO_DATE = re.compile(r"(?:19|20)\d{2}-\d{2}-\d{2}")
_BANNED_DESIGNER_KEYS = frozenset(
    {
        "symbol",
        "share_factor",
        "share_scale",
        "price_factor",
        "volume_factor",
        "date_map",
        "source_root",
        "source_glob",
        "operator_note",
        "cost_basis",
        "inspection",
        "sidecar",
        "sidecar_path",
    }
)
_BANNED_DESIGNER_TEXT = (
    "price_factor",
    "volume_factor",
    "share_scale",
    "share_factor",
    "date_map",
    "operator_note",
    "source_root",
    "source_glob",
    "sidecar",
)
# Free-text values and names use the underscore-aware ticker pattern.
# Schema keys and CSV headers do not. Status, bool, and numeric columns keep
# the tokens this report itself writes there.
_FREE_TEXT_KEYS = frozenset(
    {
        "strategy_id",
        "source_id",
        "error",
        "message",
        "note",
        "title",
        "caption",
        "software",
        "comment",
        "description",
        "label",
        "session",
        "time",
        "phase",
        "kind",
        "profile",
        "audience",
        "first_session",
        "last_session",
        "session_id",
    }
)
_BOOL_KEYS = frozenset(
    {
        "disqualified",
        "busted",
        "anonymized",
        "cancelled",
        "open_position_at_end",
    }
)
_STATUS_TOKENS = frozenset({"ok", "error", "timeout", "cancelled", "memory", "crash"})
_BOOL_TOKENS = frozenset({"true", "false"})
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# Rank is assigned after sorting. The sort is excess total return, then id.
# Desk and Arena both use that excess. Sharpe is not the rank.
OPERATOR_LEADERBOARD_COLUMNS = (
    "rank",
    "strategy_id",
    "source_id",
    "symbol",
    "profile",
    "status",
    "total_return",
    "excess_total_return",
    "bh_total_return",
    "long_2x_total_return",
    "cagr",
    "ann_vol",
    "sharpe",
    "sortino",
    "max_drawdown",
    "max_dd_duration_sessions",
    "calmar",
    "n_fills",
    "round_trips",
    "hit_rate",
    "avg_holding_sessions",
    "turnover_per_year",
    "fees_total",
    "best_day",
    "worst_day",
    "percent_up_days",
    "avg_abs_exposure_close",
    "frac_sessions_long_at_close",
    "frac_sessions_short_at_close",
    "long_round_trips",
    "short_round_trips",
    "financing_cost",
    "borrow_cost",
    "dividends_net",
    "margin_calls_filled",
    "max_gross_leverage",
    "avg_gross_leverage",
    "worst_equity_over_hwm",
    "disqualified",
    "timeout_s",
    "error",
)
DESIGNER_LEADERBOARD_COLUMNS = tuple(
    column for column in OPERATOR_LEADERBOARD_COLUMNS if column != "symbol"
)

# What ``load_report`` returns. The desktop window reads this shape.
REPORT_CONTRACT = "strategy-builder.report.v1"

# flock is per process. Threads in this process need their own lock, or two
# register_run calls in one interpreter both pass the file lock.
_INDEX_THREAD_LOCK = threading.Lock()


def percent_up_days(equity, initial):
    """Fraction of marks whose return is strictly positive.

    A previous mark of 0 has no defined return. That session is not an up day
    and it stays in the denominator. No marks returns null. The function does
    not divide by zero.
    """
    if not equity:
        return None
    previous = initial
    up = 0
    for value in equity:
        if previous != 0 and value / previous - 1.0 > 0.0:
            up += 1
        previous = value
    return up / len(equity)


def json_ready(value):
    """Strict JSON: non-finite floats become null."""
    if isinstance(value, dict):
        return {str(key): json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_ready(item) for item in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return None
    if hasattr(value, "item"):
        return json_ready(value.item())
    return value


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(json_ready(payload), indent=2, allow_nan=False)
    path.write_text(text + "\n", encoding="utf-8")


def _dates_by_day(frame) -> dict:
    out = {}
    if frame is None or "day" not in frame.columns or "date" not in frame.columns:
        return out
    for day, date in zip(frame["day"].tolist(), frame["date"].tolist()):
        out.setdefault(int(day), str(date))
    return out


def write_arena_curves(directory: Path, result: dict, frame) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    dates = _dates_by_day(frame)
    equity_rows = []
    for day, equity, position, shares, close in result["daily"]:
        equity_rows.append(
            {
                "day": int(day),
                "date": dates.get(int(day), ""),
                "equity": equity,
                "position": position,
                "shares": shares,
                "close": close,
            }
        )
    pd.DataFrame(equity_rows).to_csv(directory / "equity_curve.csv", index=False)
    _mirror_equity_csv(directory)
    fills = []
    for day, minute, session, delta, open_, fill_price, fees, shares_after in result["fills"]:
        fills.append(
            {
                "day": int(day),
                "date": dates.get(int(day), ""),
                "minute": minute,
                "session": session,
                "shares_delta": delta,
                "open": open_,
                "fill_price": fill_price,
                "fees": fees,
                "shares_after": shares_after,
            }
        )
    pd.DataFrame(fills).to_csv(directory / "fills.csv", index=False)
    pd.DataFrame(result["episodes"]).to_csv(directory / "trades.csv", index=False)


def write_desk_curves(directory: Path, result: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(result["daily"]).to_csv(directory / "equity_curve.csv", index=False)
    _mirror_equity_csv(directory)
    fill_columns = [
        "session",
        "time",
        "phase",
        "minute",
        "shares_delta",
        "price",
        "fill_price",
        "fees",
        "commission",
        "exchange",
        "clearing",
        "pass_through",
        "cat",
        "sec",
        "taf",
        "shares_after",
        "kind",
        "tier_month",
    ]
    fills = [{key: row.get(key) for key in fill_columns} for row in result["fills"]]
    pd.DataFrame(fills).to_csv(directory / "fills.csv", index=False)
    pd.DataFrame(result["episodes"]).to_csv(directory / "trades.csv", index=False)


def enrich_arena_metrics(result: dict, metrics: dict) -> dict:
    out = dict(metrics)
    equity = [row[1] for row in result["daily"]]
    out["percent_up_days"] = percent_up_days(equity, result["initial_equity"])
    return out


def write_book(directory: Path, profile: str, result: dict, metrics: dict, frame) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    if profile == "arena":
        write_arena_curves(directory, result, frame)
    elif profile == "desk":
        write_desk_curves(directory, result)
    else:
        raise ValueError(f"unknown profile {profile}")
    write_json(directory / "metrics.json", metrics)
    _plot_equity(directory / "equity.png", profile, result)
    if profile == "desk" and result.get("liquidation"):
        write_json(directory / "liquidation.json", result["liquidation"])


def _mirror_equity_csv(directory: Path) -> None:
    source = directory / "equity_curve.csv"
    (directory / "equity.csv").write_bytes(source.read_bytes())


def _equity_points(profile: str, result: dict) -> list[float]:
    daily = result.get("daily") or []
    if profile == "arena":
        return [float(row[1]) for row in daily]
    if profile == "desk":
        return [float(row["equity"]) for row in daily]
    raise ValueError(f"unknown profile {profile}")


def _plot_equity(path: Path, profile: str, result: dict) -> None:
    """Equity against the session index. The chart has no ticker and no date."""
    points = _equity_points(profile, result)
    if not points:
        return
    import matplotlib

    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(8.0, 4.5), dpi=120)
    try:
        ax.plot(range(1, len(points) + 1), points, color="#0B3A5B", linewidth=1.25)
        ax.set_xlabel("session")
        ax.set_ylabel("equity")
        ax.set_title("equity")
        ax.grid(True, color="#D0D7DE", linewidth=0.6)
        fig.tight_layout()
        path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(
            path,
            format="png",
            facecolor="white",
            metadata={"Title": "equity", "Software": "strategy-builder"},
        )
    finally:
        plt.close(fig)


def _casefold_index(text: str) -> tuple[str, list[int]]:
    """Casefold ``text`` and remember which original character each fold came from."""
    folded: list[str] = []
    origins: list[int] = []
    for index, char in enumerate(text):
        piece = char.casefold()
        folded.append(piece)
        origins.extend([index] * len(piece))
    return "".join(folded), origins


def ticker_token_spans(text: str, symbol: str) -> list[tuple[int, int]]:
    """Original spans where ``symbol`` is its own token in ``text``.

    Matching uses ``str.casefold`` on both sides, then maps the hit back onto
    ``text``. A one-character symbol uses the same non-alphanumeric boundary
    as a longer one, so ``V`` matches ``my_V_v2`` and does not match ``avg``
    or ``equity``. ``ß`` matches ``SS`` and ``ﬁ`` matches ``FI``. A character
    is a boundary when it is not alphanumeric, which is the same rule as
    ``[A-Za-z0-9]`` for ASCII. This is the only token match. Report scrubbing,
    the designer audit, and the pack scan all call it.
    """
    if not text or not symbol:
        return []
    needle = symbol.casefold()
    if not needle:
        return []
    folded, origins = _casefold_index(text)
    spans: list[tuple[int, int]] = []
    start = 0
    limit = len(folded)
    size = len(needle)
    while start <= limit - size:
        found = folded.find(needle, start)
        if found < 0:
            break
        end = found + size
        before = found == 0 or not folded[found - 1].isalnum()
        after = end == limit or not folded[end].isalnum()
        if not before or not after:
            start = found + 1
            continue
        orig_start = origins[found]
        orig_end = origins[end - 1] + 1
        if orig_end > orig_start and (not spans or orig_start >= spans[-1][1]):
            spans.append((orig_start, orig_end))
        start = end
    return spans


def _replace_token(text: str, symbol: str, replacement: str) -> str:
    spans = ticker_token_spans(text, symbol)
    if not spans:
        return text
    pieces: list[str] = []
    last = 0
    for start, end in spans:
        pieces.append(text[last:start])
        pieces.append(replacement)
        last = end
    pieces.append(text[last:])
    return "".join(pieces)


def _symbol_order(symbols) -> tuple[str, ...]:
    """Longer symbols first, so ``GOOGL`` is considered before ``GOOG``."""
    if isinstance(symbols, str):
        return (symbols,) if symbols else ()
    ordered: list[str] = []
    seen: set[str] = set()
    ranked = sorted((symbol for symbol in symbols if symbol), key=lambda item: len(item.casefold()), reverse=True)
    for symbol in ranked:
        if symbol in seen:
            continue
        seen.add(symbol)
        ordered.append(symbol)
    return tuple(ordered)


def _float_token(text: str) -> bool:
    token = text.strip()
    if not token:
        return False
    try:
        float(token)
    except ValueError:
        return False
    return True


def _exempt_designer_value(key: str, text: str) -> bool:
    """Tokens this report writes, and only in the columns that emit them."""
    folded = text.strip().casefold()
    if key == "status" and folded in _STATUS_TOKENS:
        return True
    if key in _BOOL_KEYS and folded in _BOOL_TOKENS:
        return True
    if key not in _FREE_TEXT_KEYS and key != "status" and _float_token(text):
        return True
    return False


def _boundary_tokens(text: str) -> tuple[str, ...]:
    """Alphanumeric runs of ``text``, casefolded. The same boundaries as the ticker rule."""
    folded = text.casefold()
    tokens: list[str] = []
    buf: list[str] = []
    for char in folded:
        if char.isalnum():
            buf.append(char)
        elif buf:
            tokens.append("".join(buf))
            buf = []
    if buf:
        tokens.append("".join(buf))
    return tuple(tokens)


# Canonical designer and pack paths. These spellings are never renamed.
_FIXED_REPORT_NAMES = (
    "designer",
    "equity_curve.csv",
    "equity.csv",
    "equity.png",
    "fills.csv",
    "trades.csv",
    "metrics.json",
    "liquidation.json",
    "error.json",
    "leaderboard.csv",
    "benchmarks",
    "buy_and_hold_1x",
    "long_2x",
    "manifest.json",
    "bars_part_001.csv.gz",
    "daily_rth.csv.gz",
    "referee.json",
)
# Extensions the audit would match in a file name, including ``tbl`` so a
# ticker of that extension cannot be anonymized into a report path.
_EXTENSION_TOKENS = ("csv", "gz", "json", "png", "tbl")
# Designer-facing replacement for a ticker. Operator files keep the original
# symbol. ``_designer_component`` reads this same constant. It is reserved, so
# a symbol that casefolds to ``random`` is rejected before anonymized work.
TICKER_STAND_IN = "RANDOM"
# Free text the designer audit cannot exempt: PNG Title and Software, the
# audience and profile values, session labels, the desk fill kind, and the
# sentences written into error.json. Schema keys and CSV headers are not here.
_UNEXEMPT_REPORT_TEXT = (
    "equity",
    "strategy-builder",
    "Title",
    "Software",
    "designer",
    "arena",
    "desk",
    "rth",
    "pre",
    "post",
    "margin",
    "the strategy failed",
    "withheld",
    "not published",
    "stopped",
)


def _reserved_vocabulary() -> frozenset[str]:
    found: list[str] = []
    for text in (
        *_FIXED_REPORT_NAMES,
        *_EXTENSION_TOKENS,
        TICKER_STAND_IN,
        *_UNEXEMPT_REPORT_TEXT,
    ):
        found.extend(_boundary_tokens(text))
    return frozenset(found)


RESERVED_VOCABULARY = _reserved_vocabulary()


def reserved_symbol_conflict(symbol: str) -> str | None:
    """The reserved token ``symbol`` matches, or None.

    The match is :func:`ticker_token_spans`: casefolded, bounded by a
    non-alphanumeric character. ``SS`` does not match ``random``. ``2`` does
    not match ``2x`` or ``001``.
    """
    if not isinstance(symbol, str) or symbol == "":
        return None
    for token in RESERVED_VOCABULARY:
        if ticker_token_spans(token, symbol):
            return token
    return None


def require_anonymizable_symbol(symbol: str) -> None:
    """Reject a symbol that cannot be anonymized without renaming a report file.

    The CLI prints this message and exits 2. Call it before any anonymized
    pack or designer tree is written. A raw operator run does not call it.
    """
    if reserved_symbol_conflict(symbol) is None:
        return
    raise ValueError(
        f"Symbol {symbol!r} collides with a reserved report name and cannot be anonymized; "
        "run it as an operator (raw) book or rename the symbol in the data."
    )


def designer_visible_name(symbols=()) -> str:
    """Stand-in written into a scrubbed strategy or source id.

    Fixed report files keep their canonical names. The stand-in is always
    :data:`TICKER_STAND_IN`. A symbol that casefolds to that word is rejected
    by :func:`require_anonymizable_symbol` before this runs, so there is no
    fallback spelling. Directory suffixes (``RANDOM-2``) are applied by the
    caller that reserves the folder name. ``symbols`` is unused; the batch
    cannot change the stand-in.
    """
    del symbols
    return TICKER_STAND_IN


def _designer_kind(path) -> str:
    """How to parse a designer file. Names are canonical, so the suffix decides."""
    suffix = Path(path).suffix.lower()
    if suffix == ".png":
        return "png"
    if suffix == ".json":
        return "json"
    if suffix == ".csv":
        return "csv"
    return "text"


def _safe_stand_in(symbols) -> str:
    """The replacement token. Always :data:`TICKER_STAND_IN`."""
    return designer_visible_name(symbols)


def _scrub_text(value: str, symbol: str, *, key: str = "", replacement: str | None = None) -> str:
    if replacement is None:
        replacement = designer_visible_name([symbol] if symbol else ())
    if key and _exempt_designer_value(key, value):
        return value
    text = _ISO_DATE.sub("", value)
    if not symbol:
        return text
    if text.strip().casefold() == symbol.casefold():
        return replacement
    return _replace_token(text, symbol, replacement)


def scrub_symbols(value: str, symbols, *, key: str = "") -> str:
    """Scrub every symbol in ``symbols``. A string is one symbol, not a list of characters.

    The stand-in is :data:`TICKER_STAND_IN`. A symbol that casefolds to that
    word is rejected before an anonymized book is written.
    """
    replacement = _safe_stand_in(symbols)
    text = value
    ordered = _symbol_order(symbols)
    for symbol in ordered:
        text = _scrub_text(text, symbol, key=key, replacement=replacement)
    if not ordered:
        text = _scrub_text(text, "", key=key, replacement=replacement)
    return text


def redact_metrics(payload, *, symbol: str = ""):
    """Drop the ticker, calendar dates, and scale factors from a report object."""

    def walk(value, key=""):
        if isinstance(value, dict):
            out = {}
            for child_key, item in value.items():
                if str(child_key) in _BANNED_DESIGNER_KEYS:
                    continue
                out[str(child_key)] = walk(item, str(child_key))
            return out
        if isinstance(value, (list, tuple)):
            return [walk(item, key) for item in value]
        if isinstance(value, str):
            return _scrub_text(value, symbol, key=key)
        return value

    redacted = walk(payload)
    if isinstance(redacted, dict):
        redacted["audience"] = "designer"
    return redacted


def scrub_frame_dates(frame):
    """Blank ISO dates. Session ids such as ``S00001`` stay."""
    if frame is None or "date" not in getattr(frame, "columns", ()):
        return frame
    out = frame.copy(deep=True)
    out["date"] = [
        "" if _ISO_DATE.search(str(value)) else str(value) for value in out["date"].tolist()
    ]
    return out


def _scrub_result(result: dict, symbol: str) -> dict:
    out = dict(result)
    liquidation = out.get("liquidation")
    if isinstance(liquidation, dict):
        cleaned = redact_metrics(liquidation, symbol=symbol)
        cleaned.pop("audience", None)
        out["liquidation"] = cleaned
    fills = out.get("fills")
    if fills and isinstance(fills[0], dict):
        cleaned_fills = []
        for row in fills:
            item = dict(row)
            for key in ("session", "time"):
                if isinstance(item.get(key), str):
                    item[key] = _scrub_text(item[key], symbol)
            cleaned_fills.append(item)
        out["fills"] = cleaned_fills
    daily = out.get("daily")
    if daily and isinstance(daily[0], dict):
        cleaned_daily = []
        for row in daily:
            item = dict(row)
            if isinstance(item.get("session"), str):
                item["session"] = _scrub_text(item["session"], symbol)
            cleaned_daily.append(item)
        out["daily"] = cleaned_daily
    return out


def scrub_designer_tree(
    directory,
    symbols,
    *,
    strategy_id: str = "",
    folder: str = "",
    source_id: str = "",
    source_name: str = "",
) -> None:
    """Rewrite free-text values so they use the batch's scrubbed ids.

    File names stay canonical. ``folder`` and ``source_name`` are the
    disambiguated path components, suffix included. A per-job scrub may
    already have replaced the ticker with a different stand-in than the
    batch uses. The value is still this row's id when scrubbing it and
    scrubbing the original id produce the same text, and the file then
    receives the directory component, ``-N`` included.
    """
    root = Path(directory)

    def same_row_id(value: str, original: str) -> bool:
        if not original or not value:
            return False
        if value == original:
            return True
        scrubbed = scrub_symbols(value, symbols, key="strategy_id")
        return bool(scrubbed) and scrubbed == scrub_symbols(original, symbols, key="strategy_id")

    def scrub_value(key: str, value: str) -> str:
        if key == "strategy_id" and folder and (value == folder or same_row_id(value, strategy_id)):
            return folder
        if key == "source_id" and source_name and (value == source_name or same_row_id(value, source_id)):
            return source_name
        return scrub_symbols(value, symbols, key=key)

    def walk(key: str, value):
        if isinstance(value, dict):
            return {str(child_key): walk(str(child_key), item) for child_key, item in value.items()}
        if isinstance(value, list):
            return [walk(key, item) for item in value]
        if isinstance(value, str):
            return scrub_value(key, value)
        return value

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        kind = _designer_kind(path)
        if kind == "png":
            continue
        if kind == "json":
            document = json.loads(path.read_text(encoding="utf-8"))
            rewritten = walk("", document)
            if rewritten != document:
                write_json(path, rewritten)
            continue
        if kind == "csv":
            text = path.read_text(encoding="utf-8")
            rows = list(csv.reader(io.StringIO(text)))
            if not rows:
                continue
            header = rows[0]
            changed = False
            rewritten_rows = [header]
            for row in rows[1:]:
                new_row = []
                for index, cell in enumerate(row):
                    key = header[index] if index < len(header) else ""
                    updated = scrub_value(key, cell)
                    if updated != cell:
                        changed = True
                    new_row.append(updated)
                rewritten_rows.append(new_row)
            if changed:
                buffer = io.StringIO()
                writer = csv.writer(buffer, lineterminator="\n")
                writer.writerows(rewritten_rows)
                path.write_text(buffer.getvalue(), encoding="utf-8")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        updated = scrub_value("caption", text)
        if updated != text:
            path.write_text(updated, encoding="utf-8")


def _designer_value_error(key: str, text: str, symbol: str) -> str | None:
    """Why this value cannot be shown, or None when it is safe.

    ``symbol`` is compared with free text (ids, errors, captions, names) as
    its own token, so ``my_zzzx_v2`` still matches. Other values fail only
    when the whole value is the ticker. Keys and CSV headers are not passed
    here. Status, bool, and numeric cells keep the tokens those columns emit.
    """
    if not isinstance(text, str) or text == "":
        return None
    for token in _BANNED_DESIGNER_TEXT:
        if token in text:
            return token
    if _ISO_DATE.search(text):
        return "a calendar date"
    if not symbol:
        return None
    if _exempt_designer_value(key, text):
        return None
    if text.strip().casefold() == symbol.casefold():
        return "the ticker"
    if key in _FREE_TEXT_KEYS and ticker_token_spans(text, symbol):
        return "the ticker"
    return None


def _raise_designer_value(filename: str, key: str, text: str, symbol: str) -> None:
    reason = _designer_value_error(key, text, symbol)
    if reason:
        raise RuntimeError(f"{filename} contains {reason}")


def _raise_designer_name(name: str, symbol: str) -> None:
    """File and directory names are free text. A ticker token in the name fails."""
    reason = _designer_value_error("caption", name, symbol)
    if reason == "the ticker":
        raise RuntimeError(f"{name} is named with the ticker")
    if reason:
        raise RuntimeError(f"{name} contains {reason}")


def _raise_schema_label(filename: str, label: str) -> None:
    """A JSON key or a CSV header. The ticker pattern does not apply."""
    for token in _BANNED_DESIGNER_TEXT:
        if token in label:
            raise RuntimeError(f"{filename} contains {token}")
    if _ISO_DATE.search(label):
        raise RuntimeError(f"{filename} contains a calendar date")


def _walk_designer_json(filename: str, key: str, value, symbol: str) -> None:
    if isinstance(value, dict):
        for child_key, child in value.items():
            name = str(child_key)
            _raise_schema_label(filename, name)
            _walk_designer_json(filename, name, child, symbol)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _walk_designer_json(filename, key, item, symbol)
        return
    if isinstance(value, str):
        _raise_designer_value(filename, key, value, symbol)


def _audit_json_file(path: Path, symbol: str) -> None:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{path.name} is not JSON") from exc
    _walk_designer_json(path.name, "", document, symbol)


def _audit_csv_file(path: Path, symbol: str) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"{path.name} is not text") from exc
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return
    header = rows[0]
    for label in header:
        _raise_schema_label(path.name, label)
    for row in rows[1:]:
        for index, cell in enumerate(row):
            key = header[index] if index < len(header) else ""
            _raise_designer_value(path.name, key, cell, symbol)


def _itxt_strings(data: bytes) -> list[str]:
    keyword, _, rest = data.partition(b"\x00")
    if len(rest) < 2:
        raise ValueError("truncated iTXt")
    flag = rest[0]
    method = rest[1]
    language, _, rest = rest[2:].partition(b"\x00")
    translated, _, text = rest.partition(b"\x00")
    found = [keyword.decode("latin-1", "replace")]
    if language:
        found.append(language.decode("latin-1", "replace"))
    if translated:
        found.append(translated.decode("utf-8", "replace"))
    if flag == 0:
        found.append(text.decode("utf-8", "replace"))
    elif flag == 1 and method == 0:
        found.append(zlib.decompress(text).decode("utf-8", "replace"))
    else:
        raise ValueError("unknown iTXt compression")
    return found


def _png_metadata_texts(blob: bytes) -> list[str]:
    """Uncompressed and zlib text chunks. Pixel data is not a caption."""
    if not blob.startswith(_PNG_SIGNATURE):
        raise ValueError("not a png")
    pos = 8
    texts: list[str] = []
    while pos + 12 <= len(blob):
        length = struct.unpack(">I", blob[pos : pos + 4])[0]
        kind = blob[pos + 4 : pos + 8]
        start = pos + 8
        end = start + length
        if length < 0 or end + 4 > len(blob):
            raise ValueError("truncated png")
        data = blob[start:end]
        if kind == b"tEXt":
            keyword, _, text = data.partition(b"\x00")
            texts.append(keyword.decode("latin-1", "replace"))
            texts.append(text.decode("latin-1", "replace"))
        elif kind == b"zTXt":
            keyword, _, rest = data.partition(b"\x00")
            texts.append(keyword.decode("latin-1", "replace"))
            if not rest or rest[0] != 0:
                raise ValueError("truncated zTXt")
            texts.append(zlib.decompress(rest[1:]).decode("latin-1", "replace"))
        elif kind == b"iTXt":
            texts.extend(_itxt_strings(data))
        elif kind == b"IEND":
            return texts
        pos = end + 4
    raise ValueError("png ended without IEND")


def _audit_png_file(path: Path, symbol: str) -> None:
    try:
        texts = _png_metadata_texts(path.read_bytes())
    except Exception as exc:
        raise RuntimeError(f"{path.name} png metadata could not be read") from exc
    for text in texts:
        _raise_designer_value(path.name, "caption", text, symbol)


def assert_designer_clean(directory, *, symbol: str) -> None:
    """Raise if a designer tree still carries a ticker, a date, or a factor.

    JSON and CSV are parsed. The ticker pattern is not applied to schema keys
    or CSV headers, so a column such as ``borrow_cost`` is not the ticker
    ``COST``. Free-text values and file or directory names still match an
    embedded ticker, including ``my_zzzx_v2`` and a one-character symbol in
    ``my_V_v2``. The match is casefolded, so ``ß`` is ``SS``. A value that is
    exactly the ticker fails, including PNG text and metadata, unless that
    column is where the report writes the token (``ok``, ``inf``, ``true``,
    a float). A file named ``V.json`` fails.
    """
    root = Path(directory)
    paths = [root, *list(root.rglob("*"))]
    for path in paths:
        _raise_designer_name(path.name, symbol)
        if not path.is_file():
            continue
        kind = _designer_kind(path)
        if kind == "png":
            _audit_png_file(path, symbol)
            continue
        if path.stat().st_size == 0:
            continue
        if kind == "json":
            _audit_json_file(path, symbol)
            continue
        if kind == "csv":
            _audit_csv_file(path, symbol)
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeError(f"{path.name} is not text") from exc
        _raise_designer_value(path.name, "caption", text, symbol)


def publish_designer(out_dir, profile: str, frame, books, wrapper: dict, *, symbol: str) -> Path:
    """Write the designer-facing copy under ``out_dir/designer``."""
    root = Path(out_dir) / "designer"
    if root.exists():
        shutil.rmtree(root)
    safe_frame = scrub_frame_dates(frame)
    for label, (result, metrics) in books.items():
        dest = root if label == "strategy" else root / "benchmarks" / label
        write_book(
            dest,
            profile,
            _scrub_result(result, symbol),
            redact_metrics(metrics, symbol=symbol),
            safe_frame,
        )
    write_json(root / "metrics.json", redact_metrics(wrapper, symbol=symbol))
    assert_designer_clean(root, symbol=symbol)
    return root


def _csv_cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        if math.isnan(value):
            return ""
        if math.isinf(value):
            return "inf" if value > 0.0 else "-inf"
        return format(value, ".16g")
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return str(value)


def write_leaderboard(path, rows, *, audience: str = "operator") -> None:
    """Write ``leaderboard.csv``. ``rows`` are already in rank order."""
    if audience == "operator":
        columns = OPERATOR_LEADERBOARD_COLUMNS
    elif audience == "designer":
        columns = DESIGNER_LEADERBOARD_COLUMNS
    else:
        raise ValueError("audience must be operator or designer")
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(columns)
        for row in rows:
            writer.writerow([_csv_cell(row.get(column)) for column in columns])


def read_leaderboard(path) -> list[dict]:
    with Path(path).open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _lock_index(fd: int) -> str:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        if os.fstat(fd).st_size < 1:
            os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        return "win"
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_EX)
    return "unix"


def _unlock_index(fd: int, kind: str) -> None:
    if kind == "win":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)


def _replace_index(path: Path, document: dict) -> None:
    """Write the index via a private temp file in the same directory."""
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(document, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    except Exception:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise


def register_run(index_path, run_id: str, run_dir, *, kind: str, profile: str) -> None:
    """Record ``run_id`` → directory. The index is an operator file.

    Concurrent callers share one lock file. Each writer then replaces the
    index from its own temporary file, so two runs cannot tear the JSON or
    drop a row the other one added.
    """
    path = Path(index_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(str(path) + ".lock")
    with _INDEX_THREAD_LOCK:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        kind_lock = None
        try:
            kind_lock = _lock_index(fd)
            document = {"schema_version": 1, "runs": {}}
            if path.is_file():
                document = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(document, dict) or not isinstance(document.get("runs"), dict):
                    raise ValueError(f"{path} is not a run index")
            document["runs"][str(run_id)] = {
                "path": str(Path(run_dir).resolve()),
                "kind": kind,
                "profile": profile,
            }
            _replace_index(path, document)
        finally:
            if kind_lock is not None:
                _unlock_index(fd, kind_lock)
            os.close(fd)


def resolve_run(run, *, index_path=None) -> Path:
    """A directory with a report, or an id stored in the run index."""
    candidate = Path(run)
    if candidate.is_dir() and (
        (candidate / "run.json").is_file() or (candidate / "metrics.json").is_file()
    ):
        return candidate
    index = Path(index_path) if index_path is not None else Path.cwd() / ".backtester-runs.json"
    if index.is_file():
        document = json.loads(index.read_text(encoding="utf-8"))
        entry = (document.get("runs") or {}).get(str(run))
        if entry:
            found = Path(entry["path"])
            if found.is_dir():
                return found
    raise FileNotFoundError(f"no run {run!r}")


def _redact_row(row: dict, symbol: str) -> dict:
    cleaned = {}
    for key, value in row.items():
        if key in _BANNED_DESIGNER_KEYS:
            continue
        if isinstance(value, str):
            cleaned[key] = _scrub_text(value, symbol)
        else:
            cleaned[key] = value
    return cleaned


def _coerce_cell(value):
    """Turn one leaderboard cell into the type the report contract uses.

    Empty is null. ``true`` and ``false`` are booleans. A token with a decimal
    point or an exponent is a float. A whole number is an int. Anything else
    stays text. ``inf``, ``-inf``, and ``nan`` are floats here, so a parallel
    cell is never the text ``inf``. The CSV file itself still stores that
    text; ``read_leaderboard`` does not coerce it.
    """
    if not isinstance(value, str):
        return value
    text = value.strip()
    if text == "":
        return None
    lowered = text.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in {"inf", "+inf", "infinity"}:
        return float("inf")
    if lowered in {"-inf", "-infinity"}:
        return float("-inf")
    if lowered == "nan":
        return float("nan")
    try:
        if any(mark in text for mark in (".", "e", "E")):
            return float(text)
        return int(text)
    except ValueError:
        return text


def _coerce_row(row: dict) -> dict:
    return {str(key): _coerce_cell(value) for key, value in row.items()}


def load_report(run, *, audience: str = "operator", index_path=None) -> dict:
    """Structured view of one run. ``designer`` never includes the operator secrets.

    Parallel rows use ``strategy-builder.report.v1``: numeric cells are numbers,
    flags are booleans, and a blank cell is null.
    """
    if audience not in {"operator", "designer"}:
        raise ValueError("audience must be operator or designer")
    directory = resolve_run(run, index_path=index_path)
    info_path = directory / "run.json"
    info = json.loads(info_path.read_text(encoding="utf-8")) if info_path.is_file() else {}
    kind = info.get("kind")
    if kind is None:
        if (directory / "leaderboard.csv").is_file():
            kind = "parallel"
        elif (directory / "walkforward.json").is_file():
            kind = "walkforward"
        else:
            kind = "book"
    symbol = str(info.get("symbol") or "")
    designer_root = directory / "designer"
    root = designer_root if audience == "designer" and designer_root.is_dir() else directory
    if kind == "walkforward":
        summary_path = directory / "walkforward.json"
        if not summary_path.is_file():
            raise FileNotFoundError(f"no walkforward.json in {directory}")
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        if audience == "designer":
            summary = redact_metrics(summary, symbol=symbol or str(summary.get("symbol") or ""))
        return {
            "contract": REPORT_CONTRACT,
            "audience": audience,
            "kind": "walkforward",
            "id": info.get("id"),
            "profile": info.get("profile") or summary.get("profile"),
            "summary": summary,
            "cancelled": False,
            "directory_name": directory.name if audience == "operator" else "designer",
        }
    if kind == "parallel":
        if audience == "designer" and designer_root.is_dir():
            board = designer_root / "leaderboard.csv"
        else:
            board = root / "leaderboard.csv"
        rows = read_leaderboard(board) if board.is_file() else []
        if audience == "designer" and root is directory:
            rows = [_redact_row(row, symbol) for row in rows]
        return {
            "contract": REPORT_CONTRACT,
            "audience": audience,
            "kind": "parallel",
            "id": info.get("id"),
            "profile": info.get("profile"),
            "rows": [_coerce_row(row) for row in rows],
            "cancelled": bool(info.get("cancelled")),
            "directory_name": root.name,
        }
    metrics_path = root / "metrics.json"
    if not metrics_path.is_file():
        raise FileNotFoundError(f"no metrics.json in {directory}")
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    if audience == "designer" and root is directory:
        metrics = redact_metrics(metrics, symbol=symbol or str(metrics.get("symbol") or ""))
    return {
        "contract": REPORT_CONTRACT,
        "audience": audience,
        "kind": "book",
        "id": info.get("id") or metrics.get("run_id"),
        "profile": metrics.get("profile") or info.get("profile"),
        "metrics": metrics,
        "directory_name": root.name,
    }
