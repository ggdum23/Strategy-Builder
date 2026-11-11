"""Named local 1-minute bars.

Files are ``<SYM>_1min_filled_YYYY-MM.csv.gz`` with columns
``timestamp_et,epoch_utc,open,high,low,close,volume,wap,count,filled,session``.
Prices stay raw unless the operator declares another price kind, and a
non-raw kind is recorded and refused for execution. ``share_factor`` for this
loader is 1.0. A split table is configuration: a non-empty list is rejected
rather than applied silently.

Session clock hours are inferred from the stamps. Nothing in this module
fills in a 04:00–19:59 template. The engine's ``short_session`` flag is a
separate rule (full regular close at 15:59) and is not the inferred window.

Resampling is session-anchored. A bar never contains minutes from two sessions
or two phases. A partial tail is kept and marked ``partial``.
"""

from __future__ import annotations

import math
import re
from collections import Counter
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
_PRICE_KINDS = {"raw", "total_return"}
# Column name timestamp_et claims this family of offsets. Any other offset
# contradicts the column, so the loader stops instead of renaming the zone.
_ET_OFFSETS = {"-04:00", "-05:00"}
_FULL_REGULAR_CLOSE_MINUTE = 15 * 60 + 59


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
class SessionVariant:
    """A day-shape that is not the modal window for that label."""

    start_minute: int
    end_minute: int
    bars: int
    n_sessions: int

    def as_dict(self) -> dict:
        return {
            "start_minute": self.start_minute,
            "end_minute": self.end_minute,
            "start_clock": _clock(self.start_minute),
            "end_clock": _clock(self.end_minute),
            "bars": self.bars,
            "n_sessions": self.n_sessions,
        }


@dataclass(frozen=True)
class SessionSegment:
    """Clock window inferred for one session label. Not a template."""

    label: str
    engine_phase: str
    start_minute: int
    end_minute: int
    modal_bars: int
    min_bars: int
    max_bars: int
    n_sessions: int
    variant_sessions: int
    gapped_sessions: int
    variants: tuple

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "engine_phase": self.engine_phase,
            "start_minute": self.start_minute,
            "end_minute": self.end_minute,
            "start_clock": _clock(self.start_minute),
            "end_clock": _clock(self.end_minute),
            "modal_bars": self.modal_bars,
            "min_bars": self.min_bars,
            "max_bars": self.max_bars,
            "n_sessions": self.n_sessions,
            "variant_sessions": self.variant_sessions,
            "gapped_sessions": self.gapped_sessions,
            "variants": [item.as_dict() for item in self.variants],
        }


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
    native_rows: int
    timezone_offsets: tuple
    timezone_dst_pair: bool
    session_segments: tuple
    native_tick: float
    typical_volume_bar: float
    typical_volume_session: float
    fill_policy: str
    filled_rows: int
    unfilled_rows: int
    stale_when_filled: bool | None
    phase_map: tuple
    original_session_labels: tuple
    runnable: bool

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "columns": list(self.columns),
            "session_labels": list(self.session_labels),
            "original_session_labels": list(self.original_session_labels),
            "phase_map": [{"from": src, "to": dst} for src, dst in self.phase_map],
            "timezone_note": self.timezone_note,
            "timezone_offsets": list(self.timezone_offsets),
            "timezone_dst_pair": self.timezone_dst_pair,
            "session_segments": [segment.as_dict() for segment in self.session_segments],
            "hours_assumed": False,
            "price_adjustment": self.price_adjustment,
            "splits": list(self.splits),
            "share_factor": self.share_factor,
            "native_tick": self.native_tick,
            "native_bar_minutes": self.native_bar_minutes,
            "bar_minutes": self.bar_minutes,
            "typical_volume_bar": self.typical_volume_bar,
            "typical_volume_session": self.typical_volume_session,
            "fill_policy": self.fill_policy,
            "filled_rows": self.filled_rows,
            "unfilled_rows": self.unfilled_rows,
            "stale_when_filled": self.stale_when_filled,
            "n_rows": self.n_rows,
            "native_rows": self.native_rows,
            "n_sessions": self.n_sessions,
            "first_timestamp_et": self.first_timestamp_et,
            "last_timestamp_et": self.last_timestamp_et,
            "source_glob": self.source_glob,
            "borrow_rate_annual": None,
            "cost_schedule": "chosen at run time by --profile",
            "runnable": self.runnable,
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
    extra = [str(name) for name in frame.columns if str(name) not in REQUIRED_COLUMNS]
    if extra:
        raise ValueError(f"{path.name} has extra columns: {', '.join(extra)}")
    # Names, not positions. A reordered file still lines up.
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


def _clock(minute: int) -> str:
    if isinstance(minute, bool) or not isinstance(minute, int) or not 0 <= minute <= 24 * 60 - 1:
        raise ValueError(f"minute-of-day {minute!r} is outside 00:00..23:59")
    return f"{minute // 60:02d}:{minute % 60:02d}"


def _offset_text(stamp: datetime) -> str:
    offset = stamp.utcoffset()
    if offset is None:
        raise ValueError(f"timestamp {stamp.isoformat()} has no offset")
    total = int(offset.total_seconds())
    if total % 60 != 0:
        raise ValueError(f"timestamp {stamp.isoformat()} offset is not a whole minute")
    sign = "+" if total >= 0 else "-"
    minutes = abs(total) // 60
    hours, mins = divmod(minutes, 60)
    return f"{sign}{hours:02d}:{mins:02d}"


def _median(values: list[float]) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        raise ValueError("cannot take a median of an empty sample")
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _phase_mapping(original: list[str], phase_map) -> list[str]:
    """Map file labels onto pre/rth/post. Without a map, unknown labels are an error."""
    if phase_map is None:
        mapping = {}
    elif isinstance(phase_map, dict):
        mapping = {str(key): str(value) for key, value in phase_map.items()}
    else:
        mapping = {}
        for item in phase_map:
            if len(item) != 2:
                raise ValueError(f"phase_map entry {item!r} must be a pair")
            mapping[str(item[0])] = str(item[1])
    observed = set(original)
    if mapping:
        missing = sorted(observed - set(mapping))
        if missing:
            raise ValueError(
                f"session labels {missing} have no phase_map entry. "
                "Refusing to guess which engine phase they are."
            )
        unused = sorted(set(mapping) - observed)
        if unused:
            raise ValueError(f"phase_map labels {unused} do not appear in the file")
        bad = sorted({dst for dst in mapping.values() if dst not in _PHASES})
        if bad:
            raise ValueError(
                f"phase_map targets {bad} are not engine phases. "
                f"Expected one of: {', '.join(sorted(_PHASES))}."
            )
        if len(set(mapping.values())) != len(mapping):
            raise ValueError("phase_map sends two labels to the same phase. Refusing to merge them.")
        return [mapping[label] for label in original]
    unknown = sorted(observed - _PHASES)
    if unknown:
        raise ValueError(
            f"session labels {unknown}. This loader does not assume a label set; "
            "it only accepts the pre/rth/post grid these filled files use. "
            "Pass an explicit phase_map to record a different label set. "
            "Inspect a new venue before adding it."
        )
    return list(original)


def _infer_timezone(stamps: list[datetime]) -> tuple[tuple[str, ...], bool, str]:
    offsets = sorted({_offset_text(stamp) for stamp in stamps})
    if not offsets:
        raise ValueError("no timestamps to infer a timezone from")
    foreign = sorted(set(offsets) - _ET_OFFSETS)
    if foreign:
        raise ValueError(
            "timestamp_et offsets "
            f"{offsets} contradict the column name (US Eastern is -04:00/-05:00). "
            "Refusing to guess a timezone."
        )
    if len(offsets) > 2:
        raise ValueError(f"timestamp offsets {offsets} are ambiguous")
    dst_pair = False
    if len(offsets) == 2:
        def _minutes(text: str) -> int:
            sign = 1 if text[0] == "+" else -1
            hours, mins = (int(part) for part in text[1:].split(":"))
            return sign * (hours * 60 + mins)

        if abs(_minutes(offsets[0]) - _minutes(offsets[1])) != 60:
            raise ValueError(
                f"timestamp offsets {offsets} are not a one-hour pair. Timezone is ambiguous."
            )
        dst_pair = True
    note = (
        "timestamp_et offsets observed: "
        + ", ".join(offsets)
        + ". Each bar uses its own offset. Session hours are inferred from the stamps."
    )
    return tuple(offsets), dst_pair, note


def _infer_tick(prices: list[float]) -> float:
    unique = {float(price) for price in prices}
    if len(unique) < 2:
        raise ValueError(
            "native tick is ambiguous: fewer than two distinct trade prices. "
            "Refusing to invent a tick."
        )
    scale = 100_000_000
    gcd = 0
    scaled = []
    for price in unique:
        if not math.isfinite(price):
            raise ValueError("native tick is ambiguous: a trade price is not finite")
        ticks = int(round(price * scale))
        scaled.append(ticks)
        gcd = math.gcd(gcd, abs(ticks))
    if gcd <= 0:
        raise ValueError("native tick is ambiguous: trade prices have no common grid")
    for ticks in scaled:
        if ticks % gcd != 0:
            raise ValueError("native tick is ambiguous: trade prices are not on one grid")
    tick = gcd / scale
    if tick < 1e-6:
        raise ValueError(
            f"native tick {tick} is finer than 0.000001. "
            "The trade-price grid is ambiguous."
        )
    return tick


def _infer_fill_policy(closes: list[float], volumes: list[float], filled: list[int]) -> tuple[str, int, int, bool | None]:
    n_filled = 0
    n_other = 0
    for flag in filled:
        if flag == 1:
            n_filled += 1
        elif flag == 0:
            continue
        else:
            n_other += 1
    if n_other:
        raise ValueError("filled flag is not only 0 or 1. Fill policy is ambiguous.")
    unfilled = len(filled) - n_filled
    if n_filled == 0:
        return "flag_present_none_set", 0, unfilled, None
    for index, flag in enumerate(filled):
        if flag != 1:
            continue
        if index == 0:
            raise ValueError(
                "the first bar is filled=1 and has no previous close. "
                "Fill policy is ambiguous."
            )
        price = closes[index]
        previous = closes[index - 1]
        if volumes[index] != 0.0 or price != previous:
            raise ValueError(
                "a filled=1 bar is not a stale print (volume 0 and OHLC equal to the previous close). "
                "Fill policy is ambiguous."
            )
        # OHLC equality is checked by the caller for every bar's own range.
        # A stale bar must also be flat; the previous-close check plus the
        # OHLC-contains-open rule is not enough if high differs. The caller
        # passes close only. Flatness is checked here via the volume gate and
        # the dedicated OHLC scan below when we have the rows. This loop sees
        # close. The load path checks open/high/low before calling a richer
        # scanner. See _stale_ohlc.
    return "stale_forward_fill", n_filled, unfilled, True


def _stale_bars_are_flat(frame: pd.DataFrame) -> None:
    filled = frame.loc[frame["filled"] == 1]
    if filled.empty:
        return
    flat = (
        (filled["open"] == filled["high"])
        & (filled["high"] == filled["low"])
        & (filled["low"] == filled["close"])
        & (filled["volume"] == 0.0)
    )
    if not bool(flat.all()):
        raise ValueError(
            "a filled=1 bar is not flat with zero volume. Fill policy is ambiguous."
        )


def infer_session_segments(dates, minutes, labels, phases) -> tuple[SessionSegment, ...]:
    """Infer one clock window per label from the bars that carry it.

    The window is the modal ``(first minute, last minute, bar count)`` across
    days. Other shapes are recorded as variants. There is no default session.
    """
    if not dates:
        raise ValueError("no bars to infer session hours from")
    seen_minute = set()
    by_date: dict[str, list[tuple[int, str]]] = {}
    for date, minute, label in zip(dates, minutes, labels):
        date = str(date)
        minute = int(minute)
        label = str(label)
        key = (date, minute)
        if key in seen_minute:
            raise ValueError(
                f"two bars share {date} at minute {minute}. "
                "Which session owns that minute is ambiguous."
            )
        seen_minute.add(key)
        by_date.setdefault(date, []).append((minute, label))
    for date, rows in by_date.items():
        seen_labels: list[str] = []
        previous = None
        for _minute, label in rows:
            if label == previous:
                continue
            if label in seen_labels:
                raise ValueError(
                    f"session label {label!r} is discontinuous on {date}. "
                    "Refusing to guess which block is the segment."
                )
            seen_labels.append(label)
            previous = label
    phase_of = {}
    for label, phase in zip(labels, phases):
        phase_of.setdefault(str(label), str(phase))
    per_label: dict[str, list[tuple[int, int, int, bool]]] = {}
    for date, rows in by_date.items():
        blocks: dict[str, list[int]] = {}
        for minute, label in rows:
            blocks.setdefault(label, []).append(minute)
        for label, mins in blocks.items():
            start = mins[0]
            end = mins[-1]
            gapped = end - start + 1 != len(mins)
            per_label.setdefault(label, []).append((start, end, len(mins), gapped))
    segments = []
    for label, days in per_label.items():
        signatures = [(start, end, count) for start, end, count, _gapped in days]
        counts = Counter(signatures)
        ranked = counts.most_common()
        top_count = ranked[0][1]
        tied = [signature for signature, count in ranked if count == top_count]
        # A tie is not a 04:00–19:59 guess. The listed window is the longest
        # shape, then the later close, then the earlier open. Every other
        # shape stays a variant, and the loader names the tie in its notes.
        tied.sort(key=lambda signature: (signature[2], signature[1], -signature[0]), reverse=True)
        start, end, modal_bars = tied[0]
        bar_counts = [count for _start, _end, count, _gapped in days]
        variants = []
        for signature, count in sorted(counts.items(), key=lambda item: (item[0][0], item[0][1], item[0][2])):
            if signature == (start, end, modal_bars):
                continue
            variants.append(
                SessionVariant(
                    start_minute=signature[0],
                    end_minute=signature[1],
                    bars=signature[2],
                    n_sessions=count,
                )
            )
        segments.append(
            SessionSegment(
                label=label,
                engine_phase=phase_of[label],
                start_minute=start,
                end_minute=end,
                modal_bars=modal_bars,
                min_bars=min(bar_counts),
                max_bars=max(bar_counts),
                n_sessions=len(days),
                variant_sessions=len(days) - counts[(start, end, modal_bars)],
                gapped_sessions=sum(1 for _s, _e, _c, gapped in days if gapped),
                variants=tuple(variants),
            )
        )
    segments.sort(key=lambda segment: (segment.start_minute, segment.label))
    return tuple(segments)


def load_named(src, symbol: str, *, bar_minutes: int = 1, splits=(), phase_map=None, price_kind: str = "raw") -> LoadedSeries:
    """Load filled months for one symbol. Does not copy or rewrite the source files."""
    assert_no_silent_splits(splits)
    if price_kind not in _PRICE_KINDS:
        raise ValueError(
            f"price kind {price_kind!r} is not raw or total_return. "
            "Refusing to guess whether the prints are adjusted."
        )
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
    original_labels = [str(value) for value in raw["session"].tolist()]
    try:
        phases = _phase_mapping(original_labels, phase_map)
    except ValueError as exc:
        if phase_map is None and str(exc).startswith("session labels"):
            raise ValueError(f"{symbol} has {exc}") from exc
        raise
    opens = raw["open"].astype(float)
    highs = raw["high"].astype(float)
    lows = raw["low"].astype(float)
    closes = raw["close"].astype(float)
    volumes = raw["volume"].astype(float)
    waps = raw["wap"].astype(float)
    counts = raw["count"].astype(float)
    filled_values = raw["filled"].astype(float)
    for series, name in (
        (opens, "open"),
        (highs, "high"),
        (lows, "low"),
        (closes, "close"),
        (volumes, "volume"),
        (waps, "wap"),
        (counts, "count"),
        (filled_values, "filled"),
    ):
        if not all(math.isfinite(float(value)) for value in series):
            raise ValueError(f"{symbol} has a non-finite {name}")
    if (volumes < 0).any():
        raise ValueError(f"{symbol} has negative volume")
    if ((volumes > 0) & (waps <= 0)).any():
        raise ValueError(f"{symbol} has positive volume and a non-positive wap")
    if (counts < 0).any() or any(not float(value).is_integer() for value in counts):
        raise ValueError(f"{symbol} count must be a non-negative whole number")
    if any(not float(value).is_integer() for value in filled_values):
        raise ValueError(f"{symbol} filled flag is not a whole number")
    bad_price = (opens <= 0) | (highs <= 0) | (lows <= 0) | (closes <= 0)
    if bad_price.any():
        raise ValueError(f"{symbol} has a non-positive price")
    inconsistent = (highs < lows) | (highs < opens) | (highs < closes) | (lows > opens) | (lows > closes)
    if inconsistent.any():
        raise ValueError(f"{symbol} has an OHLC bar that does not contain its open and close")
    for stamp in stamps:
        if stamp.second or stamp.microsecond:
            raise ValueError(
                f"{symbol} timestamp {stamp.isoformat()} is not minute-aligned. "
                "Native bar size is ambiguous."
            )
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
    # short_session is the engine flag from spec decision 26, measured on the
    # 1-minute grid before resampling. It is not the inferred session window.
    last_rth: dict[str, int] = {}
    for date, minute, phase, stamp, epoch in zip(dates, minutes, phases, stamps, epochs):
        if int(stamp.timestamp()) != epoch:
            raise ValueError(f"{symbol} epoch_utc does not match timestamp_et at {stamp.isoformat()}")
        if phase == "rth":
            last = last_rth.get(date)
            if last is None or minute > last:
                last_rth[date] = minute
    short = {
        date: (1 if last_rth.get(date, _FULL_REGULAR_CLOSE_MINUTE) < _FULL_REGULAR_CLOSE_MINUTE else 0)
        for date in day_of
    }
    filled_ints = [int(value) for value in filled_values]
    policy, n_filled, n_unfilled, stale_flag = _infer_fill_policy(
        [float(value) for value in closes],
        [float(value) for value in volumes],
        filled_ints,
    )
    built = pd.DataFrame(
        {
            "timestamp_et": [stamp.isoformat() for stamp in stamps],
            "epoch_utc": epochs,
            "date": dates,
            "day": day_values,
            "weekday": weekday,
            "minute": minutes,
            "time": [f"{minute // 60:02d}:{minute % 60:02d}" for minute in minutes],
            "session": phases,
            "open": opens.to_numpy(),
            "high": highs.to_numpy(),
            "low": lows.to_numpy(),
            "close": closes.to_numpy(),
            "volume": volumes.to_numpy(),
            "wap": waps.to_numpy(),
            "count": [int(value) for value in counts],
            "filled": filled_ints,
            "short_session": [short[date] for date in dates],
            "bar_minutes": 1,
            "symbol": symbol,
        }
    )
    _stale_bars_are_flat(built)
    built["partial"] = 0
    offsets, dst_pair, timezone_note = _infer_timezone(stamps)
    segments = infer_session_segments(dates, minutes, original_labels, phases)
    tick = _infer_tick([float(value) for value in list(opens) + list(highs) + list(lows) + list(closes)])
    session_volume = []
    for date in day_of:
        mask = [item == date for item in dates]
        session_volume.append(sum(float(volume) for volume, keep in zip(volumes, mask) if keep))
    typical_bar = _median([float(value) for value in volumes])
    typical_session = _median(session_volume)
    native_rows = len(built)
    if bar_minutes != 1:
        built = resample(built, bar_minutes)
    runnable = price_kind == "raw"
    if price_kind == "raw":
        price_note = "Prices are raw prints from the filled file, not dividend-adjusted and not split-adjusted."
    else:
        price_note = (
            "Operator declared total_return. Prices were not modified. "
            "The registry records the series as not runnable. "
            "A run executes it only with price basis total_return and no declared splits."
        )
    notes = [
        price_note,
        "share_factor is 1.0 for these files. Anonymized packs store their own factor in the operator sidecar.",
        "Session clock hours below were inferred from the stamps. They are not a 04:00-19:59 template.",
        "short_session is 1 when the 1-minute regular segment ends before 15:59. That flag is the engine rule, not the inferred window.",
        f"Loaded {len(paths)} monthly file(s), {native_rows} one-minute rows, delivered at {bar_minutes}-minute bars.",
        f"Fill policy: {policy}.",
        f"Native trade-price tick: {tick}.",
    ]
    for segment in segments:
        modal_sessions = segment.n_sessions - segment.variant_sessions
        if any(variant.n_sessions == modal_sessions for variant in segment.variants):
            notes.append(
                f"Session label {segment.label} has no unique modal window. "
                "The listed window is the longest shape, "
                f"{_clock(segment.start_minute)}-{_clock(segment.end_minute)}. "
                "Other shapes are recorded as variants. Hours were not assumed."
            )
        if segment.engine_phase == "rth" and segment.end_minute != _FULL_REGULAR_CLOSE_MINUTE:
            notes.append(
                "Inferred regular segment ends at "
                f"{_clock(segment.end_minute)}. The engine short_session flag still uses 15:59 "
                "as a full regular close (spec decision 26)."
            )
    if phase_map:
        notes.append("Session labels were renamed with the operator phase_map. The file's own labels are recorded.")
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
    map_pairs = tuple((str(key), str(value)) for key, value in (phase_map.items() if isinstance(phase_map, dict) else (phase_map or ())))
    inspection = Inspection(
        symbol=symbol,
        columns=tuple(REQUIRED_COLUMNS),
        session_labels=tuple(sorted(set(phases))),
        timezone_note=timezone_note,
        price_adjustment=price_kind,
        splits=tuple(splits),
        share_factor=1.0,
        native_bar_minutes=1,
        bar_minutes=bar_minutes,
        n_rows=int(len(built)),
        n_sessions=int(built["day"].nunique()),
        first_timestamp_et=str(built["timestamp_et"].iloc[0]),
        last_timestamp_et=str(built["timestamp_et"].iloc[-1]),
        source_glob=f"{symbol}_1min_filled_YYYY-MM.csv.gz",
        notes=tuple(notes),
        native_rows=native_rows,
        timezone_offsets=offsets,
        timezone_dst_pair=dst_pair,
        session_segments=segments,
        native_tick=tick,
        typical_volume_bar=typical_bar,
        typical_volume_session=typical_session,
        fill_policy=policy,
        filled_rows=n_filled,
        unfilled_rows=n_unfilled,
        stale_when_filled=stale_flag,
        phase_map=map_pairs,
        original_session_labels=tuple(sorted(set(original_labels))),
        runnable=runnable,
    )
    return LoadedSeries(frame=built, inspection=inspection)


def resample(frame: pd.DataFrame, bar_minutes: int) -> pd.DataFrame:
    """Bucket each (day, phase) segment from its own first minute.

    A trailing partial bucket is kept and marked ``partial=1``. It is not dropped,
    not padded, and not filled with the next segment. If the minute restarts
    inside a segment, that restart is a new segment and is not mixed into the
    previous bucket. A bucket is complete only when it holds ``bar_minutes``
    contiguous source minutes.
    """
    if isinstance(bar_minutes, bool) or not isinstance(bar_minutes, int) or bar_minutes < 1:
        raise ValueError("bar-minutes must be a positive integer")
    if bar_minutes == 1:
        out = frame.copy()
        out["bar_minutes"] = 1
        out["partial"] = 0
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
        complete = bucket["_n"] == bar_minutes and bucket["_last"] == bucket["minute"] + bar_minutes - 1
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
                "partial": 0 if complete else 1,
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
        "_last": int(row["minute"]),
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
    bucket["_last"] = int(row["minute"])
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
