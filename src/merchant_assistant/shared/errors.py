"""Errors raised on purpose.

Every deliberate failure is a `DomainError` with a stable `code`. Each audience sees
its own view: the user and the LLM get `public()` (the code plus a safe message), and
the logs get the detail. No raw exception text and no traceback ever cross that line.
"""

from __future__ import annotations

from typing import ClassVar


class DomainError(Exception):
    code: ClassVar[str] = "domain_error"
    user_message: ClassVar[str] = "The operation could not be completed."

    def __init__(self, detail: str = "") -> None:
        super().__init__(detail or self.code)
        # For logs only: it may carry internals, so it must never reach the user or the LLM.
        self.detail = detail

    def public(self) -> dict[str, str]:
        return {"code": self.code, "message": self.user_message}
