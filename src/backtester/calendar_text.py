"""One calendar-date decision for the screen, the export, and the publisher.

``scrub_text``, ``export_block_reason``, ``scan_publication``,
``designer_component_problem``, and ``_designer_value_error`` all use
``DATE_PATTERNS`` plus the calendar half of the strategy-id grammar.
A second list would let a spelling through one gate and fail another.

A bare year counts, including ``day 2024``. ``2024.50`` and ``0.2024`` stay
numbers. Compact eight-digit dates and two-digit-year month dates count too.
"""

from __future__ import annotations

import re

from .designer_ids import fold, id_has_calendar, is_session_id

MONTH = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
_ORDINAL = r"(?:st|nd|rd|th)"
_YEAR = r"(?:19|20)\d{2}"
_MONTH_NUM = r"(?:0[1-9]|1[0-2])"
_DAY_NUM = r"(?:0[1-9]|[12]\d|3[01])"
# Longer spellings first. A bare year is last so ``2024-01`` and ``2024-1-4`` stay one token.
# A year glued to a decimal is a number. ``day 2024`` is a date.
_BARE_YEAR = re.compile(
    rf"(?<![A-Za-z0-9.]){_YEAR}(?![A-Za-z0-9.])",
    re.IGNORECASE,
)
DATE_PATTERNS = (
    # ISO, padded or not, with an optional time: 2024-01-04, 2024-1-4.
    re.compile(
        rf"(?<!\d){_YEAR}-\d{{1,2}}-\d{{1,2}}(?!\d)"
        r"(?:[T ][0-2]\d:[0-5]\d(?::[0-5]\d)?)?"
    ),
    re.compile(rf"(?<!\d){_YEAR}[/.]\d{{1,2}}[/.]\d{{1,2}}(?!\d)"),
    # Dotted, slashed, or dashed day-month-year: 01.04.2024, 04.01.2024, 1.4.2024.
    re.compile(rf"(?<!\d)\d{{1,2}}[./-]\d{{1,2}}[./-]{_YEAR}(?!\d)"),
    re.compile(
        rf"(?<!\d)\d{{1,2}}{_ORDINAL}?[\s.\-]+{MONTH}(?![A-Za-z])\.?[\s,.\-]*{_YEAR}(?!\d)",
        re.IGNORECASE,
    ),
    # Month and day. A period alone separates them: Jan.4, 2024 and January.4 2024.
    re.compile(
        rf"(?<![A-Za-z]){MONTH}(?![A-Za-z])(?:\.\s*|\s+)\d{{1,2}}{_ORDINAL}?"
        rf"(?:(?:,\s*|\s+){_YEAR})(?!\d)",
        re.IGNORECASE,
    ),
    re.compile(rf"(?<!\d){_YEAR}-\d{{2}}(?!\d)"),
    re.compile(
        rf"(?<![A-Za-z]){MONTH}(?![A-Za-z])\.?[\s.\-]+{_YEAR}(?!\d)",
        re.IGNORECASE,
    ),
    # Compact eight-digit dates, bounded so a longer integer is not a slice.
    re.compile(rf"(?<!\d){_YEAR}{_MONTH_NUM}{_DAY_NUM}(?!\d)"),
    re.compile(rf"(?<!\d){_MONTH_NUM}{_DAY_NUM}{_YEAR}(?!\d)"),
    re.compile(rf"(?<!\d){_DAY_NUM}{_MONTH_NUM}{_YEAR}(?!\d)"),
    # Two-digit-year month dates: 4-Jan-24, Jan-24, Jan 4, 24.
    re.compile(
        rf"(?<!\d)\d{{1,2}}{_ORDINAL}?[\s.\-]+{MONTH}(?![A-Za-z])\.?[\s,.\-]*\d{{2}}(?!\d)",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?<![A-Za-z]){MONTH}(?![A-Za-z])(?:\.\s*|\s+)\d{{1,2}}{_ORDINAL}?(?:,\s*|\s+)\d{{2}}(?!\d)",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?<![A-Za-z]){MONTH}(?![A-Za-z])\.?[\s.\-]+\d{{2}}(?!\d)",
        re.IGNORECASE,
    ),
    # 01-04-24. A four-digit year is already consumed by the patterns above.
    re.compile(r"(?<!\d)\d{1,2}[./-]\d{1,2}[./-]\d{2}(?!\d)"),
    _BARE_YEAR,
)
# A whole-string epoch (``1704067200``). Shorter integers stay numbers.
_EPOCH = re.compile(r"\d{10,}\Z")
_ID_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
# One strategy id. A JSON document has quotes and braces, so it is not an id
# even when the serializer leaves out spaces.
_SINGLE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")


def _single_id(text: str) -> str | None:
    stripped = text.strip()
    if not stripped or any(char.isspace() for char in stripped):
        return None
    if _SINGLE_ID.fullmatch(stripped) is None:
        return None
    return stripped


def _whole_epoch(text: str) -> bool:
    ident = _single_id(text)
    return ident is not None and _EPOCH.fullmatch(ident) is not None


def _embedded_calendar(text: str) -> bool:
    """A calendar token in a phrase. A hex pack id is not a date."""
    for match in _ID_TOKEN.finditer(text):
        token = match.group(0)
        if is_session_id(token):
            continue
        if id_has_calendar(token, digits=False):
            return True
    return False


def contains_calendar_date(text: str) -> bool:
    """True when ``text`` contains a date spelling or a calendar strategy id."""
    folded = fold(text)
    if any(pattern.search(folded) for pattern in DATE_PATTERNS):
        return True
    if _whole_epoch(folded):
        return True
    ident = _single_id(folded)
    if ident is not None and not is_session_id(ident) and id_has_calendar(ident):
        return True
    return _embedded_calendar(folded)


def strip_calendar_dates(text: str) -> str:
    """Remove those spellings. The same decision as ``contains_calendar_date``."""
    folded = fold(text)
    ident = _single_id(folded)
    if ident is not None and not is_session_id(ident):
        if _EPOCH.fullmatch(ident) or id_has_calendar(ident) or any(
            pattern.search(ident) for pattern in DATE_PATTERNS
        ):
            return ""
    cleaned = folded
    for pattern in DATE_PATTERNS:
        cleaned = pattern.sub("", cleaned)

    def kill(match: re.Match[str]) -> str:
        token = match.group(0)
        if is_session_id(token):
            return token
        if id_has_calendar(token, digits=False):
            return ""
        return token

    return _ID_TOKEN.sub(kill, cleaned)
