"""Deterministic filled files for benchmarks and equivalence tests.

The prices sit on a one-cent grid. Weekdays come from a fixed start. A
session is 04:00–19:59 America/New_York, and every 20th session ends the
regular segment at 13:00 with no post-session bars. Nothing here is a
market download. Do not commit the files this writes.
"""

from __future__ import annotations

import gzip
import io
from datetime import date, timedelta
from pathlib import Path

_FULL_PRE = (4 * 60, 9 * 60 + 29)
_FULL_RTH = (9 * 60 + 30, 15 * 60 + 59)
_FULL_POST = (16 * 60, 19 * 60 + 59)
_SHORT_RTH = (9 * 60 + 30, 13 * 60)


def eastern_offset(day: date) -> str:
    """US Eastern offset for a bar at or after 04:00.

    DST since 2007 starts at 02:00 on the second Sunday of March and ends
    at 02:00 on the first Sunday of November. Sessions in this generator
    begin at 04:00, so the transition hour itself is never a bar.
    """
    if _nth_sunday(day.year, 3, 2) <= day < _nth_sunday(day.year, 11, 1):
        return "-04:00"
    return "-05:00"


def _nth_sunday(year: int, month: int, n: int) -> date:
    first = date(year, month, 1)
    offset = (6 - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _phase(minute: int, rth_end: int, post: bool) -> str | None:
    if _FULL_PRE[0] <= minute <= _FULL_PRE[1]:
        return "pre"
    if _FULL_RTH[0] <= minute <= rth_end:
        return "rth"
    if post and _FULL_POST[0] <= minute <= _FULL_POST[1]:
        return "post"
    return None


def session_days(sessions: int, *, start: date | None = None) -> list[date]:
    """``sessions`` weekdays beginning at ``start`` (2 Jan 2003, a Thursday)."""
    if isinstance(sessions, bool) or not isinstance(sessions, int) or sessions < 1:
        raise ValueError("sessions must be a positive integer")
    day = date(2003, 1, 2) if start is None else start
    found = []
    while len(found) < sessions:
        if day.weekday() < 5:
            found.append(day)
        day += timedelta(days=1)
    return found


def write_series(root, symbol: str = "SYN", *, sessions: int = 3, start: date | None = None) -> Path:
    """Write monthly ``<SYM>_1min_filled_YYYY-MM.csv.gz`` files under ``root``.

    Returns the directory that holds the files. ``root`` is created.
    """
    if not isinstance(symbol, str) or not symbol or "/" in symbol or "\\" in symbol:
        raise ValueError(f"invalid symbol {symbol!r}")
    destination = Path(root)
    destination.mkdir(parents=True, exist_ok=True)
    buckets: dict[tuple[int, int], list[str]] = {}
    for index, day in enumerate(session_days(sessions, start=start)):
        short = index % 20 == 19
        rth_end = _SHORT_RTH[1] if short else _FULL_RTH[1]
        offset = eastern_offset(day)
        sign = -1 if offset[0] == "-" else 1
        hours, mins = (int(part) for part in offset[1:].split(":"))
        offset_seconds = sign * (hours * 3600 + mins * 60)
        for minute in range(_FULL_PRE[0], _FULL_POST[1] + 1):
            phase = _phase(minute, rth_end, post=not short)
            if phase is None:
                continue
            cents = 10000 + index * 10 + (minute % 17)
            open_c = cents
            high_c = cents + 5
            low_c = cents - 4
            close_c = cents + 1
            volume = 100 + (minute % 50)
            epoch = _unix_naive(day.year, day.month, day.day, minute // 60, minute % 60) - offset_seconds
            text = (
                f"{day.year:04d}-{day.month:02d}-{day.day:02d}T"
                f"{minute // 60:02d}:{minute % 60:02d}:00{offset}"
            )
            row = ",".join(
                (
                    text,
                    str(epoch),
                    f"{open_c / 100:.2f}",
                    f"{high_c / 100:.2f}",
                    f"{low_c / 100:.2f}",
                    f"{close_c / 100:.2f}",
                    str(volume),
                    f"{close_c / 100:.2f}",
                    "3",
                    "0",
                    phase,
                )
            )
            buckets.setdefault((day.year, day.month), []).append(row)
    header = "timestamp_et,epoch_utc,open,high,low,close,volume,wap,count,filled,session"
    for (year, month), rows in buckets.items():
        path = destination / f"{symbol}_1min_filled_{year:04d}-{month:02d}.csv.gz"
        payload = (header + "\n" + "\n".join(rows) + "\n").encode("utf-8")
        buffer = io.BytesIO()
        with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0, filename="") as handle:
            handle.write(payload)
        path.write_bytes(buffer.getvalue())
    return destination


def _unix_naive(year: int, month: int, day: int, hour: int, minute: int, second: int = 0) -> int:
    """Unix time of a civil clock read as UTC. No leap seconds."""
    y = year - (month <= 2)
    m = month + 9 if month <= 2 else month - 3
    era = y // 400 if y >= 0 else (y - 399) // 400
    yoe = y - era * 400
    doy = (153 * m + 2) // 5 + day - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    days = era * 146097 + doe - 719468
    return days * 86400 + hour * 3600 + minute * 60 + second
