
from __future__ import annotations

import re
import unicodedata

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
ID_SHAPE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
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
    return unicodedata.normalize("NFKC", str(text)).translate(_DASH_TO_HYPHEN)


def is_session_id(text: str) -> bool:
    return _SESSION.fullmatch(text) is not None


def is_clock(text: str) -> bool:
    return _CLOCK.fullmatch(text) is not None


def is_report_filename(name: str) -> bool:
    return fold(name).casefold().endswith(_REPORT_SUFFIXES)


def _tokens(text: str) -> list[str]:
    return re.findall(r"[A-Za-z]+|\d+", text)


def _three_short_numbers(tokens: list[str]) -> bool:
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
    return _LONG_DIGITS.search(text) is not None


def strategy_id_has_calendar(text: str) -> bool:
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
    from .calendar_text import contains_calendar_date

    folded = fold(text).strip()
    if not folded:
        return "an empty name"
    if contains_calendar_date(folded) or _digit_run(folded) or id_has_calendar(folded, sessions=False):
        return "a calendar date"
    if not ID_SHAPE.fullmatch(folded):
        return "an unsafe name"
    return None
