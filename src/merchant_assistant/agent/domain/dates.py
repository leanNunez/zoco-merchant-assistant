"""Which calendar dates a conversation actually stated, so the model cannot guess one.

The brief asks the agent to ask back when a date is relative ("ayer", "el viernes pasado") or
ambiguous ("03/04"). The `consultar_ventas` contract only checks the ISO shape, so a model
could turn "ayer" into `2026-10-22` by itself and the call would pass. This module is the
deterministic check the turn applies before running the tool: the date must be one the user
wrote unambiguously, or one the assistant stated and the user then accepted with a strict yes.

What counts as an explicit date in a USER message (`explicit_dates`):

- ISO `2026-10-22`;
- `d/m/yyyy` or `d-m-yyyy` with a four-digit year, ONLY when the reading is unambiguous: the
  first number is above 12 (so it must be the day) or the day equals the month. `3/4/2026`
  could be the 3rd of April or the 4th of March, so it is not extracted;
- Spanish long forms: `22 de octubre de 2026`, `22 octubre 2026`, `octubre 22, 2026`
  (accents and case do not matter; `setiembre` is accepted).

What it does NOT do, on purpose: it never resolves a relative expression ("ayer", "hoy", "el
viernes pasado", "la semana pasada"), never fills in a missing year ("22 de octubre"), never
reads a two-digit year, and never reads `m/d/yyyy` (US order). Every one of those is a question
to ask the user, not a date to extract. The cost of being this strict is that a legitimate
request can be refused once and the agent has to ask; that is the safe failure.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from datetime import date

from merchant_assistant.agent.domain.messages import Message, Role
from merchant_assistant.agent.domain.notes import is_platform_note
from merchant_assistant.agent.domain.replies import ReplyKind, classify_reply

MONTHS = {
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
_MONTH = "|".join(MONTHS)
_LAST_MONTH_OF_YEAR = 12

# [0-9], not \d: \d also matches digits of other scripts. The lookarounds keep a date from
# being read out of a longer run of digits (a ticket number, a phone).
_ISO = re.compile(r"(?<![0-9])([0-9]{4})-([0-9]{2})-([0-9]{2})(?![0-9])")
_NUMERIC = re.compile(r"(?<![0-9])([0-9]{1,2})([/-])([0-9]{1,2})\2([0-9]{4})(?![0-9])")
_LONG_DAY_FIRST = re.compile(
    rf"(?<![0-9])([0-9]{{1,2}})\s+(?:de\s+)?({_MONTH})\b(?:\s*,\s*|\s+)(?:(?:de|del)\s+)?"
    r"([0-9]{4})(?![0-9])"
)
_LONG_MONTH_FIRST = re.compile(rf"\b({_MONTH})\s+([0-9]{{1,2}})\s*,?\s+([0-9]{{4}})(?![0-9])")


def explicit_dates(text: str) -> frozenset[date]:
    """Every unambiguous, real calendar date written in `text`. Relative ones never are."""
    normalized = _normalize(text)
    found: set[date] = set()
    found.update(_iso_dates(normalized))
    for match in _NUMERIC.finditer(normalized):
        first, second, year = int(match.group(1)), int(match.group(3)), int(match.group(4))
        if first > _LAST_MONTH_OF_YEAR or first == second:
            _add(found, year, second, first)
    found.update(_long_dates(normalized))
    return frozenset(found)


def dates_stated_by_assistant(text: str) -> frozenset[date]:
    """The dates an assistant message states, for the user to accept or reject.

    The assistant is told to write dates as ISO or as day/month/year, so here a numeric
    `dd/mm/yyyy` is read day first even when the first number is 12 or less: the user is
    accepting the date exactly as the assistant wrote it, in the order the assistant uses.
    """
    normalized = _normalize(text)
    found: set[date] = set()
    found.update(_iso_dates(normalized))
    for match in _NUMERIC.finditer(normalized):
        if match.group(2) == "/":
            _add(found, int(match.group(4)), int(match.group(3)), int(match.group(1)))
    found.update(_long_dates(normalized))
    return frozenset(found)


def is_grounded(day: date, history: Sequence[Message]) -> bool:
    """Whether `day` comes from the user and not from the model's own interpretation.

    True when a USER message wrote the date explicitly, or when the LAST user message is a
    strict yes and the message right before it is an ASSISTANT message stating that date
    (the assistant proposed an interpretation and the user accepted it).
    """
    if any(day in explicit_dates(message.content or "") for message in _user_messages(history)):
        return True
    return day in accepted_assistant_dates(history)


def accepted_assistant_dates(history: Sequence[Message]) -> frozenset[date]:
    """The dates of the assistant message the latest user message accepted with a strict yes."""
    proposal = assistant_message_before_last_user(history)
    last_user = _last_user(history)
    if proposal is None or last_user is None:
        return frozenset()
    if classify_reply(last_user.content or "") is not ReplyKind.AFFIRMATIVE:
        return frozenset()
    return dates_stated_by_assistant(proposal.content or "")


def assistant_message_before_last_user(history: Sequence[Message]) -> Message | None:
    """The assistant text the latest user message answers, if that is what precedes it."""
    for index in range(len(history) - 1, -1, -1):
        if history[index].role is Role.USER:
            # A platform note the code wrote between the two is not part of what was said.
            for previous in reversed(history[:index]):
                if is_platform_note(previous):
                    continue
                if previous.role is Role.ASSISTANT and previous.content:
                    return previous
                return None
            return None
    return None


def _last_user(history: Sequence[Message]) -> Message | None:
    users = _user_messages(history)
    return users[-1] if users else None


def _user_messages(history: Sequence[Message]) -> list[Message]:
    return [message for message in history if message.role is Role.USER]


def _iso_dates(normalized: str) -> set[date]:
    found: set[date] = set()
    for match in _ISO.finditer(normalized):
        _add(found, int(match.group(1)), int(match.group(2)), int(match.group(3)))
    return found


def _long_dates(normalized: str) -> set[date]:
    found: set[date] = set()
    for match in _LONG_DAY_FIRST.finditer(normalized):
        _add(found, int(match.group(3)), MONTHS[match.group(2)], int(match.group(1)))
    for match in _LONG_MONTH_FIRST.finditer(normalized):
        _add(found, int(match.group(3)), MONTHS[match.group(1)], int(match.group(2)))
    return found


def _add(found: set[date], year: int, month: int, day: int) -> None:
    try:
        found.add(date(year, month, day))
    except ValueError:
        return  # 31/02/2026 is not a date, so it is not a date the user stated


def _normalize(text: str) -> str:
    """NFKC (full-width digits become ASCII), accents removed, casefolded."""
    decomposed = unicodedata.normalize("NFD", unicodedata.normalize("NFKC", text))
    return "".join(char for char in decomposed if unicodedata.category(char) != "Mn").casefold()
