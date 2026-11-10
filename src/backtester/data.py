"""Named local 1-minute bars.

Files are ``<SYM>_1min_filled_YYYY-MM.csv.gz`` with columns
``timestamp_et,epoch_utc,open,high,low,close,volume,wap,count,filled,session``.
Prices stay raw. ``share_factor`` for this loader is 1.0. A split table is
configuration: a non-empty list is rejected rather than applied silently.

Resampling is session-anchored. A bar never contains minutes from two sessions
or two phases (pre / rth / post).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

REQUIRED_COLUMNS = (
    "timestamp_et",
    "epoch_utc",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "wap",
    "count",
    "filled",
    "session",
)

_FILE_RE = re.compile(r"^(.+)_1min_filled_(\d{4})-(\d{2})\.csv\.gz$")
_PHASES = {"pre", "rth", "post"}


def monthly_filled_filename(symbol: str, year: int, month: int) -> str:
    """File name Greg's filled trees use. The symbol may contain a dot (BRK.B)."""
    if not isinstance(symbol, str) or not symbol or "/" in symbol or "\\" in symbol:
        raise ValueError(f"invalid symbol {symbol!r}")
    if not isinstance(year, int) or isinstance(year, bool) or year < 1900:
        raise ValueError("year must be an integer")
    if not isinstance(month, int) or isinstance(month, bool) or not 1 <= month <= 12:
        raise ValueError("month must be 1..12")
    return f"{symbol}_1min_filled_{year:04d}-{month:02d}.csv.gz"


def monthly_filled_path(root, symbol: str, year: int, month: int):
    """Join ``root / filename`` without assuming the path flavour.

    ``root`` may be a ``pathlib.Path`` or a ``PureWindowsPath``. The result
    stays on that flavour, so a Windows path can be built on Linux.
    """
    return root / monthly_filled_filename(symbol, year, month)


def assert_no_silent_splits(splits) -> None:
    """Raw mode does not adjust prices. An explicit split list must not be ignored."""
    if splits:
        raise ValueError(
            "Split tables are configuration and are not applied in this phase. "
            "Prices are the raw prints in the file. Pass an empty split list, "
            "or do not pass one."
        )


@dataclass(frozen=True)
class Inspection:
    """What a series actually is, written down before anyone runs a strategy on it."""

    symbol: str
    columns: tuple
    session_labels: tuple
    timezone_note: str
    price_adjustment: str
    splits: tuple
    share_factor: float
    native_bar_minutes: int
    bar_minutes: int
    n_rows: int
    n_sessions: int
    first_timestamp_et: str
    last_timestamp_et: str
    source_glob: str
    notes: tuple

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "columns": list(self.columns),
            "session_labels": list(self.session_labels),
            "timezone_note": self.timezone_note,
            "price_adjustment": self.price_adjustment,
            "splits": list(self.splits),
            "share_factor": self.share_factor,
            "native_bar_minutes": self.native_bar_minutes,
            "bar_minutes": self.bar_minutes,
            "n_rows": self.n_rows,
            "n_sessions": self.n_sessions,
            "first_timestamp_et": self.first_timestamp_et,
            "last_timestamp_et": self.last_timestamp_et,
            "source_glob": self.source_glob,
            "borrow_rate_annual": None,
            "cost_schedule": "chosen at run time by --profile",
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class LoadedSeries:
    frame: pd.DataFrame
    inspection: Inspection


@dataclass(frozen=True)
class WalkSlices:
    design_end_day: int
    design_end_date: str
    locked_start_day: int
    locked_start_date: str
    warmup_days_requested: int
    warmup_sessions: int
    design: pd.DataFrame
    locked: pd.DataFrame
    locked_with_warmup: pd.DataFrame


def discover_filled_files(src, symbol: str) -> list[Path]:
    """Monthly files under ``src``, or ``src`` itself when it is one file."""
    root = Path(src)
    if root.is_file():
        match = _FILE_RE.match(root.name)
        if match is None or match.group(1) != symbol:
            raise FileNotFoundError(
                f"{root} is not {monthly_filled_filename(symbol, 2000, 1).split('_1min')[0]}"
                f"_1min_filled_YYYY-MM.csv.gz"
            )
        return [root]
    if not root.is_dir():
        raise FileNotFoundError(f"data source does not exist: {root}")
    found = sorted(path for path in root.glob(f"{symbol}_1min_filled_*.csv.gz") if path.is_file())
    parsed = []
    for path in found:
        match = _FILE_RE.match(path.name)
        if match is None or match.group(1) != symbol:
            continue
        parsed.append((match.group(2), match.group(3), path))
    parsed.sort()
    if not parsed:
        raise FileNotFoundError(
            f"no {symbol}_1min_filled_YYYY-MM.csv.gz files in {root}"
        )
    return [path for _, _, path in parsed]


def absent_months(paths, symbol: str) -> tuple[str, ...]:
    """YYYY-MM labels with no file, from the earliest file through the latest.

    A holiday gap inside a file that exists is not a missing month. One file,
    or a run of consecutive months, returns an empty tuple.
    """
    keys = []
    for path in paths:
        match = _FILE_RE.match(Path(path).name)
        if match is None or match.group(1) != symbol:
            continue
        keys.append(int(match.group(2)) * 12 + int(match.group(3)) - 1)
    if len(keys) < 2:
        return ()
    have = set(keys)
    missing = []
    for index in range(min(keys), max(keys) + 1):
        if index not in have:
            year, month0 = divmod(index, 12)
            missing.append(f"{year:04d}-{month0 + 1:02d}")
    return tuple(missing)


def _month_label(index: int) -> str:
    year, month0 = divmod(index, 12)
    return f"{year:04d}-{month0 + 1:02d}"


def _read_month(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    missing = [name for name in REQUIRED_COLUMNS if name not in frame.columns]
    if missing:
        raise ValueError(f"{path.name} is missing columns: {', '.join(missing)}")
    return frame.loc[:, list(REQUIRED_COLUMNS)].copy()


def _as_utc_stamp(value) -> datetime:
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"cannot parse timestamp_et {value!r}") from exc
    if stamp.tzinfo is None:
        raise ValueError(f"timestamp_et {value!r} has no offset")
    return stamp


def load_dividends(path) -> dict[str, float]:
    """CSV with columns ``session,amount``. ``session`` matches the desk session id (YYYY-MM-DD)."""
    frame = pd.read_csv(path)
    if list(frame.columns)[:2] != ["session", "amount"] and not {"session", "amount"} <= set(frame.columns):
        raise ValueError("dividends file needs columns session,amount")
    out = {}
    for session, amount in zip(frame["session"], frame["amount"]):
        key = str(session)
        value = float(amount)
        if not math.isfinite(value):
            raise ValueError(f"dividend for {key} is not finite")
        if key in out:
            raise ValueError(f"duplicate dividend session {key}")
        out[key] = value
    return out


def load_named(src, symbol: str, *, bar_minutes: int = 1, splits=()) -> LoadedSeries:
    """Load raw filled months for one symbol. Does not copy or rewrite the source files."""
    assert_no_silent_splits(splits)
    if isinstance(bar_minutes, bool) or not isinstance(bar_minutes, int) or bar_minutes < 1:
        raise ValueError("bar-minutes must be a positive integer")
    paths = discover_filled_files(src, symbol)
    frames = [_read_month(path) for path in paths]
    raw = pd.concat(frames, ignore_index=True)
    if raw.empty:
        raise ValueError(f"no rows in {symbol} filled files")
    stamps = [_as_utc_stamp(value) for value in raw["timestamp_et"]]
    epochs = [int(value) for value in raw["epoch_utc"]]
    for index in range(1, len(epochs)):
        if epochs[index] <= epochs[index - 1]:
            raise ValueError(
                f"{symbol} timestamps are not strictly increasing at row {index} ({raw['timestamp_et'].iloc[index]})"
            )
    phases = raw["session"].astype(str)
    unknown = sorted(set(phases.unique()) - _PHASES)
    if unknown:
        raise ValueError(
            f"{symbol} has session labels {unknown}. This loader does not assume a label set; "
            "it only accepts the pre/rth/post grid these filled files use. Inspect a new venue before adding it."
        )
    opens = raw["open"].astype(float)
    highs = raw["high"].astype(float)
    lows = raw["low"].astype(float)
    closes = raw["close"].astype(float)
    volumes = raw["volume"].astype(float)
    if (volumes < 0).any():
        raise ValueError(f"{symbol} has negative volume")
    bad_price = (opens <= 0) | (highs <= 0) | (lows <= 0) | (closes <= 0)
    if bad_price.any():
        raise ValueError(f"{symbol} has a non-positive price")
    inconsistent = (highs < lows) | (highs < opens) | (highs < closes) | (lows > opens) | (lows > closes)
    if inconsistent.any():
        raise ValueError(f"{symbol} has an OHLC bar that does not contain its open and close")
    dates = [stamp.date().isoformat() for stamp in stamps]
    minutes = [stamp.hour * 60 + stamp.minute for stamp in stamps]
    # Session index in file order. ISO dates sort with time, and the file is monotonic.
    day_of = {}
    day_values = []
    for date in dates:
        if date not in day_of:
            day_of[date] = len(day_of)
        day_values.append(day_of[date])
    weekday = [datetime.fromisoformat(date).weekday() for date in dates]
    # short_session is measured on the 1-minute grid, before resampling relabels
    # bucket starts. A full session's last RTH bar starts at 15:59 (minute 959).
    last_rth: dict[str, int] = {}
    for date, minute, phase, stamp, epoch in zip(dates, minutes, phases.tolist(), stamps, epochs):
        if int(stamp.timestamp()) != epoch:
            raise ValueError(f"{symbol} epoch_utc does not match timestamp_et at {stamp.isoformat()}")
        if phase == "rth":
            last = last_rth.get(date)
            if last is None or minute > last:
                last_rth[date] = minute
    short = {date: (1 if last_rth.get(date, 959) < 959 else 0) for date in day_of}
    built = pd.DataFrame(
        {
            "timestamp_et": [stamp.isoformat() for stamp in stamps],
            "epoch_utc": epochs,
            "date": dates,
            "day": day_values,
            "weekday": weekday,
            "minute": minutes,
            "time": [f"{minute // 60:02d}:{minute % 60:02d}" for minute in minutes],
            "session": phases.tolist(),
            "open": opens.to_numpy(),
            "high": highs.to_numpy(),
            "low": lows.to_numpy(),
            "close": closes.to_numpy(),
            "volume": volumes.to_numpy(),
            "wap": raw["wap"].astype(float).to_numpy(),
            "count": raw["count"].astype(int).to_numpy(),
            "filled": raw["filled"].astype(int).to_numpy(),
            "short_session": [short[date] for date in dates],
            "bar_minutes": 1,
            "symbol": symbol,
        }
    )
    native_rows = len(built)
    if bar_minutes != 1:
        built = resample(built, bar_minutes)
    notes = [
        "Prices are raw prints from the filled file, not dividend-adjusted and not split-adjusted.",
        "share_factor is 1.0 for raw files. Do not use the anonymized desk kit's 0.9 on this frame.",
        "session is pre/rth/post from the file. The desk engine maps the calendar date to its session id.",
        "short_session is 1 when the 1-minute RTH segment ends before 15:59. It is not inferred from a ticker calendar.",
        f"Loaded {len(paths)} monthly file(s), {native_rows} one-minute rows, delivered at {bar_minutes}-minute bars.",
    ]
    missing = absent_months(paths, symbol)
    if missing:
        span = []
        for path in paths:
            match = _FILE_RE.match(Path(path).name)
            if match is not None and match.group(1) == symbol:
                span.append(int(match.group(2)) * 12 + int(match.group(3)) - 1)
        notes.append(
            "Missing monthly file(s) between "
            f"{_month_label(min(span))} and {_month_label(max(span))}: {', '.join(missing)}. "
            "Files that exist were loaded. A gap inside a file that exists is not a missing month."
        )
    notes = tuple(notes)
    inspection = Inspection(
        symbol=symbol,
        columns=tuple(REQUIRED_COLUMNS),
        session_labels=tuple(sorted(set(built["session"].tolist()))),
        timezone_note="timestamp_et carries an explicit offset. The column name is US Eastern; the offset is what is used.",
        price_adjustment="raw",
        splits=tuple(splits),
        share_factor=1.0,
        native_bar_minutes=1,
        bar_minutes=bar_minutes,
        n_rows=int(len(built)),
        n_sessions=int(built["day"].nunique()),
        first_timestamp_et=str(built["timestamp_et"].iloc[0]),
        last_timestamp_et=str(built["timestamp_et"].iloc[-1]),
        source_glob=f"{symbol}_1min_filled_YYYY-MM.csv.gz",
        notes=notes,
    )
    return LoadedSeries(frame=built, inspection=inspection)


def resample(frame: pd.DataFrame, bar_minutes: int) -> pd.DataFrame:
    """Bucket each (day, phase) segment from its own first minute.

    A trailing partial bucket is kept. If the minute restarts inside a segment,
    that restart is a new segment and is not mixed into the previous bucket.
    """
    if isinstance(bar_minutes, bool) or not isinstance(bar_minutes, int) or bar_minutes < 1:
        raise ValueError("bar-minutes must be a positive integer")
    if bar_minutes == 1:
        out = frame.copy()
        out["bar_minutes"] = 1
        return out.reset_index(drop=True)
    records = frame.to_dict("records")
    segments = []
    current = []
    prev_key = None
    prev_minute = None
    for row in records:
        key = (row["day"], row["session"])
        minute = int(row["minute"])
        if prev_key is None or key != prev_key or minute <= prev_minute:
            if current:
                segments.append(current)
            current = [row]
            prev_key = key
        else:
            current.append(row)
        prev_minute = minute
    if current:
        segments.append(current)
    output = []
    for segment in segments:
        output.extend(_resample_segment(segment, bar_minutes))
    return pd.DataFrame(output)


def _resample_segment(segment: list[dict], bar_minutes: int) -> list[dict]:
    anchor = int(segment[0]["minute"])
    anchor_stamp = _as_utc_stamp(segment[0]["timestamp_et"])
    anchor_epoch = int(segment[0]["epoch_utc"])
    buckets = []
    current = None
    current_minute = None
    for row in segment:
        minute = int(row["minute"])
        offset = minute - anchor
        if offset < 0:
            raise ValueError("resample offset became negative")
        bucket_minute = anchor + (offset // bar_minutes) * bar_minutes
        if current is None or bucket_minute != current_minute:
            if current is not None:
                buckets.append(current)
            current = _new_bucket(row, bucket_minute, anchor, anchor_stamp, anchor_epoch)
            current_minute = bucket_minute
        else:
            _accumulate(current, row)
    if current is not None:
        buckets.append(current)
    finished = []
    for bucket in buckets:
        volume = bucket["volume"]
        if volume > 0.0:
            wap = bucket["_pv"] / volume
        else:
            wap = bucket["close"]
        filled = 1 if bucket["_n"] == bucket["_filled_n"] and bucket["_n"] > 0 else 0
        finished.append(
            {
                "timestamp_et": bucket["timestamp_et"],
                "epoch_utc": bucket["epoch_utc"],
                "date": bucket["date"],
                "day": bucket["day"],
                "weekday": bucket["weekday"],
                "minute": bucket["minute"],
                "time": f"{bucket['minute'] // 60:02d}:{bucket['minute'] % 60:02d}",
                "session": bucket["session"],
                "open": bucket["open"],
                "high": bucket["high"],
                "low": bucket["low"],
                "close": bucket["close"],
                "volume": volume,
                "wap": wap,
                "count": bucket["count"],
                "filled": filled,
                "short_session": bucket["short_session"],
                "bar_minutes": bar_minutes,
                "symbol": bucket["symbol"],
            }
        )
    return finished


def _new_bucket(row, bucket_minute, anchor, anchor_stamp, anchor_epoch):
    shift = bucket_minute - anchor
    stamp = anchor_stamp + timedelta(minutes=shift)
    return {
        "timestamp_et": stamp.isoformat(),
        "epoch_utc": anchor_epoch + shift * 60,
        "date": row["date"],
        "day": int(row["day"]),
        "weekday": int(row["weekday"]),
        "minute": bucket_minute,
        "session": row["session"],
        "open": float(row["open"]),
        "high": float(row["high"]),
        "low": float(row["low"]),
        "close": float(row["close"]),
        "volume": float(row["volume"]),
        "count": int(row["count"]),
        "short_session": int(row["short_session"]),
        "symbol": row.get("symbol", ""),
        "_pv": float(row["wap"]) * float(row["volume"]),
        "_n": 1,
        "_filled_n": int(row["filled"]),
    }


def _accumulate(bucket, row):
    high = float(row["high"])
    low = float(row["low"])
    if high > bucket["high"]:
        bucket["high"] = high
    if low < bucket["low"]:
        bucket["low"] = low
    bucket["close"] = float(row["close"])
    bucket["volume"] += float(row["volume"])
    bucket["count"] += int(row["count"])
    bucket["_pv"] += float(row["wap"]) * float(row["volume"])
    bucket["_n"] += 1
    bucket["_filled_n"] += int(row["filled"])
    if int(row["weekday"]) != int(bucket["weekday"]):
        raise ValueError("weekday changed inside a resampled session")


def frame_to_desk_bars(frame: pd.DataFrame) -> list[dict]:
    """Desk bars. ``session`` is the real date; ``phase`` is pre/rth/post.

    Column ``count`` is pulled by name. Iterating tuples would rename it,
    because ``count`` clashes with a tuple method.
    """
    rows = []
    previous = None
    columns = {name: frame[name].tolist() for name in (
        "date", "session", "minute", "time", "open", "high", "low", "close", "volume", "wap", "count", "filled",
    )}
    n = len(frame)
    for i in range(n):
        session_id = str(columns["date"][i])
        rows.append(
            {
                "session": session_id,
                "phase": columns["session"][i],
                "minute": int(columns["minute"][i]),
                "time": columns["time"][i],
                "open": float(columns["open"][i]),
                "high": float(columns["high"][i]),
                "low": float(columns["low"][i]),
                "close": float(columns["close"][i]),
                "volume": float(columns["volume"][i]),
                "wap": float(columns["wap"][i]),
                "count": int(columns["count"][i]),
                "filled": int(columns["filled"][i]),
                "new_session": session_id != previous,
            }
        )
        previous = session_id
    return rows


def slice_walkforward(
    frame: pd.DataFrame,
    *,
    design_end_day: int | None = None,
    design_end_date: str | None = None,
    design_fraction: float | None = None,
    warmup_days: int = 0,
) -> WalkSlices:
    """Split a frame into a design window and a locked tail.

    The design frame contains no bar from the locked tail. Warm-up rows for the
    locked run, when requested, are strictly earlier than the locked start.
    There is no default cut.
    """
    choices = [design_end_day is not None, design_end_date is not None, design_fraction is not None]
    if sum(choices) != 1:
        raise ValueError("pass exactly one of design_end_day, design_end_date, design_fraction")
    if isinstance(warmup_days, bool) or not isinstance(warmup_days, int) or warmup_days < 0:
        raise ValueError("warmup_days must be a non-negative integer")
    days = list(dict.fromkeys(int(day) for day in frame["day"].tolist()))
    if not days:
        raise ValueError("frame has no sessions")
    if days != sorted(days):
        raise ValueError("day index is not chronological")
    day_of_date = {}
    date_of_day = {}
    for day, date in zip(frame["day"].tolist(), frame["date"].tolist()):
        day = int(day)
        date = str(date)
        date_of_day.setdefault(day, date)
        day_of_date.setdefault(date, day)
    if design_fraction is not None:
        fraction = float(design_fraction)
        if not 0.0 < fraction < 1.0:
            raise ValueError("design_fraction must be strictly between 0 and 1")
        n_design = math.floor(len(days) * fraction)
        if n_design < 1 or n_design >= len(days):
            raise ValueError("design_fraction leaves an empty design window or an empty locked tail")
        design_end = days[n_design - 1]
    elif design_end_date is not None:
        if str(design_end_date) not in day_of_date:
            raise ValueError(f"design_end_date {design_end_date} is not in the sample")
        design_end = day_of_date[str(design_end_date)]
    else:
        design_end = int(design_end_day)
    if design_end not in date_of_day:
        raise ValueError(f"design_end_day {design_end} is not in the sample")
    if design_end == days[-1]:
        raise ValueError("design end leaves an empty locked tail")
    if design_end < days[0]:
        raise ValueError("design end leaves an empty design window")
    design = frame.loc[frame["day"] <= design_end].reset_index(drop=True)
    locked = frame.loc[frame["day"] > design_end].reset_index(drop=True)
    locked_start = int(locked["day"].iloc[0])
    warm_start = locked_start - warmup_days
    locked_with = frame.loc[(frame["day"] >= warm_start) & (frame["day"] <= int(locked["day"].iloc[-1]))].reset_index(drop=True)
    warm = locked_with.loc[locked_with["day"] < locked_start]
    actual = int(warm["day"].nunique()) if len(warm) else 0
    if int(design["day"].max()) > design_end:
        raise RuntimeError("design slice contains a bar after the design end")
    if int(locked["day"].min()) <= design_end:
        raise RuntimeError("locked slice contains a design bar")
    if int(locked_with["day"].max()) > int(locked["day"].max()):
        raise RuntimeError("warmup slice runs past the locked window")
    if actual and int(warm["day"].max()) > design_end:
        raise RuntimeError("warmup contains a locked bar")
    future = set(int(day) for day in design["day"].unique()) & set(int(day) for day in locked["day"].unique())
    if future:
        raise RuntimeError(f"design and locked slices share days {sorted(future)}")
    return WalkSlices(
        design_end_day=design_end,
        design_end_date=date_of_day[design_end],
        locked_start_day=locked_start,
        locked_start_date=date_of_day[locked_start],
        warmup_days_requested=warmup_days,
        warmup_sessions=actual,
        design=design,
        locked=locked,
        locked_with_warmup=locked_with,
    )
