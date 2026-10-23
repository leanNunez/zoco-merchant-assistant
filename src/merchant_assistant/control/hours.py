"""The human attention window.

The brief does not define it, so it is configuration injected by the composition root;
this module hard-codes no default.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, tzinfo


@dataclass(frozen=True)
class BusinessHours:
    """Weekly window in a timezone. Monday is 0. `opens` is inclusive, `closes` exclusive."""

    tz: tzinfo
    opens: time
    closes: time
    weekdays: frozenset[int]

    def __post_init__(self) -> None:
        if self.opens.tzinfo is not None or self.closes.tzinfo is not None:
            raise ValueError("opens and closes are wall-clock times: pass naive time objects")
        if not self.opens < self.closes:
            raise ValueError("opens must be earlier than closes: overnight windows are unsupported")
        if not self.weekdays:
            raise ValueError("weekdays must not be empty")
        if not all(0 <= day <= 6 for day in self.weekdays):
            raise ValueError("weekdays must be within 0 (Monday) and 6 (Sunday)")

    def is_open(self, at: datetime) -> bool:
        """Whether humans attend at this instant, judged in the business timezone."""
        if at.utcoffset() is None:
            raise ValueError("is_open needs a timezone-aware datetime")
        local = at.astimezone(self.tz)
        return local.weekday() in self.weekdays and self.opens <= local.time() < self.closes
