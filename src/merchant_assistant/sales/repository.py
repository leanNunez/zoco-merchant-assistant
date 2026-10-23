"""The port through which sales are read."""

from __future__ import annotations

from datetime import date
from typing import Protocol

from merchant_assistant.sales.models import DailySales


class SalesRepository(Protocol):
    def get(self, day: date) -> DailySales | None:
        """The day's sales, or None when nothing was registered for it."""
        ...
