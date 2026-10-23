"""Which period a question is about, and whether a document was valid in it.

The corpus has rules that replaced others (DOC-12 is the history of DOC-05 and DOC-07),
so the right document depends on WHEN the question is about, not only on what it says.
A question that names a month, a date or a year is about that period; one that names
none is about today. The detection is deliberately small and literal: it only reads
what the question states, and never guesses relative expressions such as "el mes
pasado" (those are for the agent to clarify with the user).
"""

from __future__ import annotations

import calendar
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Literal

from merchant_assistant.knowledge.documents import Document
from merchant_assistant.knowledge.text import normalize

PeriodSource = Literal["question", "today"]
_Span = tuple[date, date]
_SpanBuilder = Callable[[re.Match[str]], _Span | None]

_MONTHS = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "septiembre": 9,
    "setiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}
# Only 1900-2099 counts as a year, so "$10.000" or "3000 entradas" are not read as years.
# "de 2000 entradas" still is: a known limit of reading dates without understanding them.
_YEAR = r"(?:19|20)\d{2}"
_MONTH_YEAR = re.compile(rf"\b({'|'.join(_MONTHS)})\s+(?:(?:de|del)\s+)?({_YEAR})\b")
_DAY_MONTH_YEAR = re.compile(rf"\b(\d{{1,2}})/(\d{{1,2}})/({_YEAR})\b")
_ISO_DATE = re.compile(rf"\b({_YEAR})-(\d{{2}})-(\d{{2}})\b")
_BARE_YEAR = re.compile(rf"\b(?:en|de|del)\s+({_YEAR})\b")


@dataclass(frozen=True, slots=True)
class Period:
    start: date
    end: date
    source: PeriodSource

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise ValueError(f"a period cannot start ({self.start}) after it ends ({self.end})")


def reference_period(query: str, today: date) -> Period:
    """The span of every period the query mentions, or `today` if it mentions none.

    Mentions are read in order of precedence and each one consumes its text, so the year
    of "octubre de 2026" is not read again as a bare "de 2026": month and year, then
    explicit dates (dd/mm/yyyy and yyyy-mm-dd), then a year after "en", "de" or "del".
    Several mentions give one span from the earliest start to the latest end. An
    impossible date such as 31/02/2026 is not a mention.
    """
    text = normalize(query)
    spans: list[_Span] = []

    def consume(pattern: re.Pattern[str], to_span: _SpanBuilder) -> None:
        nonlocal text
        for match in pattern.finditer(text):
            span = to_span(match)
            if span is not None:
                spans.append(span)
        # Blank out what was read, keeping the length so later matches stay word-aligned.
        text = pattern.sub(lambda match: " " * len(match.group()), text)

    consume(_MONTH_YEAR, _month_span)
    consume(_DAY_MONTH_YEAR, _day_month_year_span)
    consume(_ISO_DATE, _iso_span)
    consume(_BARE_YEAR, _year_span)
    if not spans:
        return Period(today, today, "today")
    return Period(min(s for s, _ in spans), max(e for _, e in spans), "question")


def is_valid_during(document: Document, period: Period) -> bool:
    """True if the document's validity overlaps the period (both ends inclusive)."""
    return document.valid_from <= period.end and document.valid_until >= period.start


def _month_span(match: re.Match[str]) -> _Span:
    year, month = int(match.group(2)), _MONTHS[match.group(1)]
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def _day_month_year_span(match: re.Match[str]) -> _Span | None:
    return _day(int(match.group(3)), int(match.group(2)), int(match.group(1)))


def _iso_span(match: re.Match[str]) -> _Span | None:
    return _day(int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _year_span(match: re.Match[str]) -> _Span:
    year = int(match.group(1))
    return date(year, 1, 1), date(year, 12, 31)


def _day(year: int, month: int, day: int) -> _Span | None:
    try:
        moment = date(year, month, day)
    except ValueError:
        return None
    return moment, moment
