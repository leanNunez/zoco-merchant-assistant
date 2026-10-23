"""The date contract of `consultar_ventas`.

Strict on purpose: the tool rejects what it does not understand instead of guessing. A date
the model could only have guessed ("ayer", "03/04") is the model's job to resolve with the
customer, not this module's job to interpret.
"""

from __future__ import annotations

import re
from datetime import date

from merchant_assistant.sales.errors import FutureSalesDate, InvalidSalesDate

# [0-9], not \d: \d also matches digits of other scripts, which int() would happily accept.
_ISO_DAY = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
_PREVIEW = 24


def parse_iso_day(raw: str) -> date:
    """A calendar date written exactly as YYYY-MM-DD. Says nothing about today."""
    match = _ISO_DAY.fullmatch(raw)
    if match is None:
        # Not echoing the whole input: it is untrusted and can be arbitrarily long.
        raise InvalidSalesDate(f"not in YYYY-MM-DD format: {_preview(raw)} ({len(raw)} characters)")
    year, month, day = (int(group) for group in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        raise InvalidSalesDate(f"not a calendar date: {_preview(raw)}") from None


def parse_sales_date(raw: str, *, today: date) -> date:
    """The date to look up. `today` is the business day, so today itself is allowed."""
    day = parse_iso_day(raw)
    if day > today:
        raise FutureSalesDate(f"{day} is after {today}")
    return day


def _preview(raw: str) -> str:
    shown = raw[:_PREVIEW]
    return repr(shown + "..." if len(raw) > _PREVIEW else shown)
