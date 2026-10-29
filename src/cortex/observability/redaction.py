"""What may appear in a trace (brief section 7): an allowlist, a scrubber and a salted principal.

Traces must not carry secrets or full sensitive data. Three layers, applied where the event is
written so that nothing unfiltered reaches any destination:

1. Allowlist: each event type names the attributes it may carry. Anything else is dropped (and
   counted in `dropped_attributes`), so a new attribute is private until somebody allows it.
2. Scrubber: whatever string passes is stripped of API keys, bearer tokens, e-mail addresses,
   phone numbers, card numbers, CUIT and DNI. Values come from the code (names, codes, ids), but
   an id chosen by the model could still carry a secret.
3. Principal: only a salted hash of who acted is traced, never the id itself.

Message text never enters a trace; only its length does.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from types import MappingProxyType

Scalar = str | int | bool | None

REDACTED = "[redacted]"
MAX_VALUE_CHARS = 120

# Order matters: the specific number formats go before the generic digit runs.
_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:sk|pk|rk|ghp|gho|xox[abp])[-_][A-Za-z0-9_-]{8,}"),  # provider API keys
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
    re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),  # e-mail
    re.compile(r"\d{9,}"),  # a long run of digits, even glued to letters (card, CUIT, phone)
    re.compile(r"\b\d{2}-\d{8}-\d\b"),  # CUIT with dashes
    re.compile(r"(?<![\w-])\d(?:[ .-]?\d){6,}(?![\w-])"),  # card, phone, CUIT, DNI: 7+ digits
    re.compile(r"(?<![\w-])[A-Za-z0-9_-]{32,}(?![\w-])"),  # any other long opaque token
)


def scrub(text: str, limit: int = MAX_VALUE_CHARS) -> str:
    """The text without secrets or personal numbers, cut to `limit` characters."""
    for pattern in _PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text[:limit]


def scrub_tree(value: object, limit: int = 1000) -> object:
    """The same masking applied to every string inside nested lists and dictionaries."""
    if isinstance(value, str):
        return scrub(value, limit)
    if isinstance(value, Mapping):
        return {key: scrub_tree(item, limit) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [scrub_tree(item, limit) for item in value]
    return value


def _allowed(*names: str) -> frozenset[str]:
    return frozenset(names)


# Event type -> the attributes it may carry. A type that is not listed carries none.
ALLOWED_ATTRIBUTES: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "run_started": _allowed("principal"),
        "checkpoint_failed": _allowed("mode"),
        "model_called": _allowed(),
        "model_failed": _allowed("code"),
        "model_replied": _allowed("kind"),
        "tool_requested": _allowed("tool"),
        "tool_rejected": _allowed("tool", "reason", "detail"),
        "tool_executed": _allowed("tool", "data_tenant"),
        "tool_error": _allowed("tool", "reason", "data_tenant"),
        "tool_retry": _allowed("tool", "attempt", "reason"),
        "tool_deduplicated": _allowed("tool", "data_tenant"),
        "tool_confirmation_required": _allowed("tool", "operation_id"),
        "confirmation_received": _allowed("tool", "operation_id"),
        "confirmation_cancelled": _allowed("tool", "operation_id"),
        "write_reconciled": _allowed("tool", "data_tenant"),
        "write_uncertain": _allowed("tool", "operation_id"),
        "write_attempt_failed": _allowed("tool", "reason"),
        "write_lookup": _allowed("tool", "found"),
        "reply_suppressed": _allowed("chars"),
        "final_answer": _allowed("chars"),
        "followup_decided": _allowed("decision", "reasons"),
        "chat_event_rejected": _allowed("reason"),
        "run_finished": _allowed(
            "finish_reason",
            "finish_detail",
            "steps",
            "tools_executed",
            "tools_rejected",
            "retries",
            "duration_ms",
        ),
    }
)


class TraceRedactor:
    def __init__(self, salt: str) -> None:
        if not salt:
            raise ValueError("the principal hash needs a salt")
        self._salt = salt

    def principal(self, principal_id: str) -> str:
        digest = hashlib.sha256(f"{self._salt}\n{principal_id}".encode()).hexdigest()
        return f"p_{digest[:10]}"

    def clean(
        self, event_type: str, attributes: Mapping[str, Scalar]
    ) -> tuple[dict[str, Scalar], int]:
        """The attributes that may be traced, scrubbed, and how many were dropped."""
        allowed = ALLOWED_ATTRIBUTES.get(event_type, frozenset())
        kept: dict[str, Scalar] = {}
        dropped = 0
        for name, value in attributes.items():
            if name not in allowed:
                dropped += 1
            else:
                kept[name] = scrub(value) if isinstance(value, str) else value
        return kept, dropped
