"""Time source.

Production code never reads the process clock: ruff's DTZ rules reject a naive
`datetime.now()` and `date.today()`. Whatever needs "now" receives a `Clock`.

The business day is the one in the business timezone, which can differ from the
server's: a UTC container is already "tomorrow" at 21:00 in Buenos Aires.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, tzinfo
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """The current instant. Always timezone-aware."""
        ...


@dataclass(frozen=True)
class SystemClock:
    """The real clock, in UTC. Convert with `business_today` for business dates."""

    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass(frozen=True)
class FixedClock:
    """A frozen instant, for tests and for reproducible evaluation runs."""

    instant: datetime

    def __post_init__(self) -> None:
        if self.instant.utcoffset() is None:
            raise ValueError("FixedClock needs a timezone-aware datetime")

    def now(self) -> datetime:
        return self.instant


def business_today(clock: Clock, tz: tzinfo) -> date:
    """The calendar date right now in the business timezone."""
    return clock.now().astimezone(tz).date()
