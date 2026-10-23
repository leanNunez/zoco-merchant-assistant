"""Why a sales lookup was refused.

Each error has a stable `code` and a safe `user_message` that tells the caller what to
fix. The `detail` is for the logs only: it may name a file or quote the rejected input.
"""

from __future__ import annotations

from typing import ClassVar

from merchant_assistant.shared.errors import DomainError


class SalesError(DomainError):
    """Base of every refusal raised by the sales capability."""


class InvalidSalesDate(SalesError):  # noqa: N818 - named after the situation
    """The date is not a real calendar date written as YYYY-MM-DD."""

    code: ClassVar[str] = "invalid_date"
    user_message: ClassVar[str] = "The date must be written in the YYYY-MM-DD format."


class FutureSalesDate(SalesError):  # noqa: N818 - named after the situation
    """Sales cannot exist for a day that has not happened yet."""

    code: ClassVar[str] = "future_date"
    user_message: ClassVar[str] = "Sales can only be looked up for today or an earlier date."


class InvalidSalesData(SalesError):  # noqa: N818 - named after the situation
    """The sales file is missing, unreadable or inconsistent."""

    code: ClassVar[str] = "invalid_sales_data"
    user_message: ClassVar[str] = "The sales data could not be read."
