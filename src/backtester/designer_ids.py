"""Allow-list for a designer-visible strategy id.

The publisher decides on the original id, before a ticker rewrite or a date
scrub. A second predicate is how ``alpha_2024-01-04_v2`` used to be published
as ``alpha__v2``. Free text still uses ``calendar_text``; the calendar half of
this grammar is that same decision.
"""

from __future__ import annotations

import re
import unicodedata

# The same dash fold as the screen. Fullwidth digits fold under NFKC.
_DASH_TO_HYPHEN = str.maketrans(
    {
        "\u2212": "-",
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\ufe63": "-",
        "\uff0d": "-",
    }
)
# Leading letter, then letters, digits, underscore, or hyphen. 64 characters.
ID_SHAPE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
# A decimal is a number (``2024.50``, ``0.2024``). Any other run of four
# digits is a year, a compact date, an epoch, or a long number.
_LONG_DIGITS = re.compile(r"(?<![\d.])\d{4,}(?![\d.])")
_SESSION = re.compile(r"[SBsb]\d+\Z")
_CLOCK = re.compile(r"(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?\Z")
_REPORT_SUFFIXES = (".json", ".csv", ".png", ".txt", ".md", ".gz")
_MONTHS = frozenset(
    "jan january feb february mar march apr april may jun june jul july aug august "
    "sep sept september oct october nov november dec december".split()
)
_CALENDAR_WORDS = frozenset(
    {
        "today",
        "holiday",
        "yesterday",
        "tomorrow",
        "eod",
        "mon",
        "monday",
        "tue",
        "tues",
        "tuesday",
        "wed",
        "wednesday",
        "thu",
        "thur",
        "thurs",
        "thursday",
        "fri",
        "friday",
        "sat",
        "saturday",
        "sun",
        "sunday",
    }
)
_QUARTER_DIGIT = frozenset({"1", "2", "3", "4"})


def fold(text: str) -> str:
    """NFKC, then unicode minus and dash characters become ASCII ``-``."""
    return unicodedata.normalize("NFKC", str(text)).translate(_DASH_TO_HYPHEN)


def is_session_id(text: str) -> bool:
    """Engine session labels (``S00001``, ``B01``). Not strategy ids."""
    return _SESSION.fullmatch(text) is not None


def is_clock(text: str) -> bool:
    return _CLOCK.fullmatch(text) is not None


def is_report_filename(name: str) -> bool:
    """A report file. The id shape is not applied to ``metrics.json``."""
    return fold(name).casefold().endswith(_REPORT_SUFFIXES)


def _tokens(text: str) -> list[str]:
    """Split on punctuation and on letter/digit boundaries."""
    return re.findall(r"[A-Za-z]+|\d+", text)


def _three_short_numbers(tokens: list[str]) -> bool:
    """``01-04-24`` and ``1_4_24``. ``ma_20_50`` has only two."""
    run = 0
    for token in tokens:
        if token.isdigit() and len(token) <= 2:
            run += 1
            if run >= 3:
                return True
        else:
            run = 0
    return False


def _calendar_markers(tokens: list[str]) -> bool:
    """Month and calendar words, quarters next to digits, and fy/h/week prefixes.

    Bare ``q1``, ``h1``, ``h2``, and ``fy`` stay. ``2024Q1``, ``h12``, and
    ``fy2024`` do not. ``long_2x`` is not an ``h`` token.
    """
    for index, token in enumerate(tokens):
        low = token.casefold()
        if low in _MONTHS or low in _CALENDAR_WORDS:
            return True
        if low == "q" and index + 1 < len(tokens) and tokens[index + 1] in _QUARTER_DIGIT:
            if any(other.isdigit() for other_index, other in enumerate(tokens) if other_index not in {index, index + 1}):
                return True
        if low in {"fy", "wk", "week"} and index + 1 < len(tokens) and tokens[index + 1].isdigit():
            return True
        if low == "h" and index + 1 < len(tokens) and tokens[index + 1][:1] in {"1", "2"}:
            digits = tokens[index + 1]
            if len(digits) > 1 or (index + 2 < len(tokens) and tokens[index + 2].isdigit()):
                return True
    return False


def id_has_calendar(text: str, *, sessions: bool = True, digits: bool = True) -> bool:
    """Calendar half of the id grammar. Pure digit strings are not decided here.

    Session labels are exempt when ``sessions`` is true, so an equity file can
    keep ``S00001``. ``digits`` is false for a token inside a phrase, so a hex
    pack id is not a date. A strategy id still fails the four-digit run: the
    publisher passes ``sessions=False``. A clock is not three numeric tokens.
    """
    if is_clock(text):
        return False
    if sessions and is_session_id(text):
        return False
    if text.isdigit():
        return False
    if digits and _LONG_DIGITS.search(text):
        return True
    return _calendar_markers(_tokens(text)) or _three_short_numbers(_tokens(text))


def _digit_run(text: str) -> bool:
    """Four or more digits, including a pure number. A decimal is not a run."""
    return _LONG_DIGITS.search(text) is not None


def strategy_id_has_calendar(text: str) -> bool:
    """Calendar half only, on the original id, before a ticker rewrite."""
    from .calendar_text import contains_calendar_date

    folded = fold(text).strip()
    if not folded:
        return False
    return bool(
        contains_calendar_date(folded)
        or _digit_run(folded)
        or id_has_calendar(folded, sessions=False)
    )


def strategy_id_problem(text: str) -> str | None:
    """Why this original id cannot be a designer directory, or None.

    Calendar hits win over the shape check, so ``20240104`` is a date.
    ``vol_1.5`` is an unsafe name. ``QQQ1`` passes; the ticker rule runs
    after this, on the scrubbed id.
    """
    from .calendar_text import contains_calendar_date

    folded = fold(text).strip()
    if not folded:
        return "an empty name"
    # Sessions are not free-text dates, and they are not strategy ids either.
    if contains_calendar_date(folded) or _digit_run(folded) or id_has_calendar(folded, sessions=False):
        return "a calendar date"
    if not ID_SHAPE.fullmatch(folded):
        return "an unsafe name"
    return None
