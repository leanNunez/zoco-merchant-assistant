"""What may appear in a trace (brief section 7): an allowlist, a scrubber and a keyed pseudonym.

Traces must not carry secrets or full sensitive data. Three layers, applied where the event is
written so that nothing unfiltered reaches any destination:

1. Allowlist: each event type names the attributes it may carry. Anything else is dropped (and
   counted in `dropped_attributes`), so a new attribute is private until somebody allows it.
2. Scrubber: whatever string passes is stripped of API keys, bearer tokens, e-mail addresses,
   phone numbers, card numbers, CUIT and DNI. Values come from the code (names, codes, ids), but
   an id chosen by the model could still carry a secret.
3. Principal: only a keyed pseudonym of who acted is traced, never the id itself. It is an
   HMAC-SHA-256 of company and principal under a key the entry point chooses.

Message text never enters a trace; only its length does.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping
from types import MappingProxyType

Scalar = str | int | bool | None

REDACTED = "[redacted]"
MAX_VALUE_CHARS = 120

# Every pattern runs in linear time on any input (tests/cortex/unit/test_redaction.py feeds each
# one hostile 200 KB strings). A backtracking engine turns quadratic when a candidate that fails
# is retried from each of its inner positions, so the two patterns that start on a run of
# characters (the e-mail and the digit chain) end in a second alternative that eats the failed
# run whole. That is not a shortcut: a later start inside a run that failed from its beginning
# sees the same ending and fails the same way, so the matches are exactly those of the naive
# pattern. The `hit` group tells the two alternatives apart.
#
# Order matters: the specific number formats go before the generic digit runs.
_KEYS = re.compile(r"\b(?:sk|pk|rk|ghp|gho|xox[abp])[-_][A-Za-z0-9_-]{8,}")  # provider API keys
_BEARER = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)
_EMAIL = re.compile(r"(?:(?P<hit>[\w.+-]+@[\w-]+(?:\.[\w-]+)+)|[\w.+-]++)")
_LONG_DIGITS = re.compile(r"\d{9,}")  # a long run of digits, even glued to letters
_CUIT = re.compile(r"\b\d{2}-\d{8}-\d\b")
# Card, phone, CUIT, DNI: 7+ digits that may be separated by one space, dot or dash.
_DIGIT_CHAIN = re.compile(r"(?<![\w-])(?:(?P<hit>\d(?:[ .-]?\d){6,}(?![\w-]))|\d(?:[ .-]?\d)*+)")
_OPAQUE = re.compile(r"(?<![\w-])[A-Za-z0-9_-]{32,}(?![\w-])")  # any other long opaque token


def _mask_hits(pattern: re.Pattern[str], text: str) -> str:
    """Mask what the `hit` group matched and leave the runs the pattern only skipped over."""
    return pattern.sub(lambda m: REDACTED if m.group("hit") is not None else m.group(0), text)


def scrub(text: str, limit: int = MAX_VALUE_CHARS) -> str:
    """The text without secrets or personal numbers, cut to `limit` characters.

    The whole text is masked first and cut afterwards: cutting first could leave half a secret
    visible. That is why the masking itself has to be linear.
    """
    text = _BEARER.sub(REDACTED, _KEYS.sub(REDACTED, text))
    text = _mask_hits(_EMAIL, text)
    text = _CUIT.sub(REDACTED, _LONG_DIGITS.sub(REDACTED, text))
    text = _mask_hits(_DIGIT_CHAIN, text)
    text = _OPAQUE.sub(REDACTED, text)
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
        "tool_error": _allowed(
            "tool", "reason", "transient", "attempt", "uncertain", "data_tenant"
        ),
        "tool_retry": _allowed("tool", "attempt", "reason"),
        "tool_deduplicated": _allowed("tool", "data_tenant"),
        "tool_confirmation_required": _allowed("tool", "operation_id"),
        "confirmation_received": _allowed("tool", "operation_id"),
        "confirmation_cancelled": _allowed("tool", "operation_id"),
        "write_reconciled": _allowed("tool", "data_tenant"),
        "write_uncertain": _allowed("tool", "operation_id"),
        "write_lookup": _allowed("tool", "found"),
        "reply_suppressed": _allowed("chars"),
        "final_answer": _allowed("chars"),
        "followup_decided": _allowed("decision", "reasons"),
        "followup_replayed": _allowed("decision"),
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
    def __init__(self, key: str) -> None:
        if not key.strip():
            raise ValueError("the principal pseudonym needs a key")
        self._key = key.encode("utf-8")

    def principal(self, tenant_id: str, principal_id: str) -> str:
        """A stable pseudonym of who acted, in that company.

        The company is part of the message, so the same principal id in two companies gets two
        pseudonyms. The NUL byte that joins them cannot appear in an identifier (their alphabet
        is closed), so no two (company, principal) pairs encode to the same message.
        """
        message = f"{tenant_id}\0{principal_id}".encode()
        digest = hmac.new(self._key, message, hashlib.sha256).hexdigest()
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
