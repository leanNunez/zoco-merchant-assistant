"""Why a complaint was refused or could not be stored.

Each error has a stable `code` and a safe `user_message`. The `detail` is for the logs
only: it can quote the customer's text or name a file, so it never reaches the customer
or the LLM.
"""

from __future__ import annotations

from typing import ClassVar

from merchant_assistant.shared.errors import DomainError


class ComplaintError(DomainError):
    """Base of every refusal raised by the complaints capability."""


class InvalidComplaint(ComplaintError):  # noqa: N818 - named after the situation
    """The reason or the description does not meet the contract of `crear_reclamo`."""

    code: ClassVar[str] = "invalid_complaint"
    user_message: ClassVar[str] = (
        "The reason must have 3 to 80 characters and the description 10 to 1000."
    )


class ComplaintStoreError(ComplaintError):
    """The complaints database failed. Whether the complaint was saved is unknown."""

    code: ClassVar[str] = "complaint_store_error"
    user_message: ClassVar[str] = "The complaint could not be saved right now."
