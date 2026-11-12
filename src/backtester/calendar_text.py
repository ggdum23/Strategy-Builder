"""One calendar-date decision for the screen, the export, and the publisher.

``scrub_text``, ``export_block_reason``, ``scan_publication``,
``designer_component_problem``, and ``_designer_value_error`` all use
``DATE_PATTERNS``. A second list would let a spelling through one gate
and fail another.

A bare year counts, including a year next to any word other than ``day``.
``day 2024`` stays. ``today 2024`` and ``holiday 2024`` do not. A year
glued to a decimal (``2024.50``, ``0.2024``) is a number, not a date.
"""

from __future__ import annotations

import re

MONTH = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
_ORDINAL = r"(?:st|nd|rd|th)"
# Longer spellings first. A bare year is last so ``2024-01`` and ``2024-1-4`` stay one token.
# A year glued to a decimal is a number. A bare year stays only when the whole
# preceding word is "day" (``today 2024`` and ``holiday 2024`` still lose the year).
_BARE_YEAR = re.compile(
    r"(?<![A-Za-z0-9.])(?<!\bday )(?:19|20)\d{2}(?![A-Za-z0-9.])",
    re.IGNORECASE,
)
DATE_PATTERNS = (
    # ISO, padded or not, with an optional time: 2024-01-04, 2024-1-4.
    re.compile(
        r"(?<!\d)(?:19|20)\d{2}-\d{1,2}-\d{1,2}(?!\d)"
        r"(?:[T ][0-2]\d:[0-5]\d(?::[0-5]\d)?)?"
    ),
    re.compile(r"(?<!\d)(?:19|20)\d{2}[/.]\d{1,2}[/.]\d{1,2}(?!\d)"),
    # Dotted, slashed, or dashed day-month-year: 01.04.2024, 04.01.2024, 1.4.2024.
    re.compile(r"(?<!\d)\d{1,2}[./-]\d{1,2}[./-](?:19|20)\d{2}(?!\d)"),
    re.compile(
        rf"(?<!\d)\d{{1,2}}[\s.\-]+{MONTH}(?![A-Za-z])\.?[\s,.\-]*(?:19|20)\d{{2}}(?!\d)",
        re.IGNORECASE,
    ),
    # Month and day. A period alone separates them: Jan.4, 2024 and January.4 2024.
    re.compile(
        rf"(?<![A-Za-z]){MONTH}(?![A-Za-z])(?:\.\s*|\s+)\d{{1,2}}{_ORDINAL}?"
        rf"(?:(?:,\s*|\s+)(?:19|20)\d{{2}})(?!\d)",
        re.IGNORECASE,
    ),
    re.compile(r"(?<!\d)(?:19|20)\d{2}-\d{2}(?!\d)"),
    re.compile(
        rf"(?<![A-Za-z]){MONTH}(?![A-Za-z])\.?[\s.\-]+(?:19|20)\d{{2}}(?!\d)",
        re.IGNORECASE,
    ),
    _BARE_YEAR,
)


def contains_calendar_date(text: str) -> bool:
    """True when ``text`` contains a spelling in ``DATE_PATTERNS``."""
    return any(pattern.search(text) for pattern in DATE_PATTERNS)


def strip_calendar_dates(text: str) -> str:
    """Remove those spellings. The same patterns ``contains_calendar_date`` uses."""
    cleaned = text
    for pattern in DATE_PATTERNS:
        cleaned = pattern.sub("", cleaned)
    return cleaned
