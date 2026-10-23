"""What the sales capability knows about a day.

Money is `Decimal`, never float: 0.1 + 0.2 must not become 0.30000000000000004 in a report
a merchant reads. A refund can exceed the day's gross sales (refunds of earlier days), so a
negative net is valid.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class DailySales:
    day: date
    tickets: int
    gross: Decimal
    refunds: Decimal
    net: Decimal

    def __post_init__(self) -> None:
        if self.tickets < 0:
            raise ValueError(f"{self.day}: tickets cannot be negative, got {self.tickets}")
        for name, amount in (("gross", self.gross), ("refunds", self.refunds), ("net", self.net)):
            if not amount.is_finite():
                raise ValueError(f"{self.day}: {name} must be a finite amount, got {amount}")
        if self.gross < 0:
            raise ValueError(f"{self.day}: gross cannot be negative, got {self.gross}")
        if self.refunds < 0:
            raise ValueError(f"{self.day}: refunds cannot be negative, got {self.refunds}")
        if self.net != self.gross - self.refunds:
            raise ValueError(
                f"{self.day}: net {self.net} is not gross {self.gross} minus refunds {self.refunds}"
            )


@dataclass(frozen=True, slots=True)
class SalesLookup:
    """The answer to "how did we sell on this day?".

    `record is None` means no sales were registered for that day. That is different from a
    registered day with zero sales, and the agent must not present one as the other.
    """

    day: date
    record: DailySales | None

    def __post_init__(self) -> None:
        if self.record is not None and self.record.day != self.day:
            raise ValueError(
                f"the lookup is for {self.day} but the record is for {self.record.day}"
            )
