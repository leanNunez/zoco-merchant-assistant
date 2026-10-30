"""The independent checker: did the scenario really end the way it should have?

It does not import the harness. It reads the scenario's `expected` block, the adapter's JSON
output and, when the world was kept, the SQLite files themselves, and it recomputes the effects
with its own queries. An adapter that reported something different from what is in the files, or
broke a rule of the contract, fails here even when its `expected` values happen to match.

Every `expected` key needs a rule below. A key nobody knows how to check fails loudly: an
assertion that is not checked would turn a PASS into a guess.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

KIT_CONFIG = Path(__file__).resolve().parents[2] / "kit" / "harness_config.json"


def read_allowed_finish_reasons(config: Path) -> frozenset[str]:
    """The finish reasons the contract allows, as the kit's own configuration lists them. The
    checker keeps no copy of its own that could drift from the file."""
    try:
        reasons = json.loads(config.read_text(encoding="utf-8")).get("allowed_finish_reasons")
    except (OSError, ValueError, AttributeError):
        reasons = None
    if not isinstance(reasons, list) or not all(isinstance(reason, str) for reason in reasons):
        raise ValueError(f"{config.name} has no list of allowed_finish_reasons")
    return frozenset(reasons)


ALLOWED_FINISH_REASONS = read_allowed_finish_reasons(KIT_CONFIG)
REQUIRED_OUTPUT = (
    "run_id",
    "tenant_id",
    "conversation_id",
    "finish_reason",
    "step_count",
    "events",
)
# Fault tokens that make nothing fail: a plain success, and the same command arriving twice
# (the external system absorbs it by its key). Every other token is a failure the run must report.
NON_FAILING_FAULTS = frozenset({"SUCCESS", "REPEAT_SAME_OPERATION_ID"})
# What a secret or a personal identifier looks like. This is the checker's own copy, written for
# the same shapes the trace scrubber masks (API keys, bearer tokens, e-mails, long digit runs,
# cards, CUIT) and not shared with it: the checker must not import the system it judges.
# A bare number of 6 to 8 digits is NOT a secret here, because outputs carry amounts.
SECRET = re.compile(
    "|".join(
        (
            r"\b(?:sk|pk|rk|ghp|gho|xox[abp])[-_][A-Za-z0-9_-]{8,}",  # provider API keys
            r"(?i:\bBearer)\s+[A-Za-z0-9._~+/=-]{8,}",
            r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+",  # e-mail
            r"\d{9,}",  # a long run of digits, even glued to letters
            r"\b\d{2}-\d{8}-\d\b",  # CUIT with dashes
            r"(?<![\w-])\d(?:[ .-]?\d){12,18}(?![\w-])",  # card-like groups of 13 to 19 digits
        )
    )
)
# Any other long opaque token. Only the values are searched for it: the contract's own keys
# (delivered_ai_messages_after_takeover) are as long as a token.
OPAQUE_TOKEN = re.compile(r"(?<![\w-])[A-Za-z0-9_-]{32,}(?![\w-])")


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    ok: bool
    detail: str = ""


# --------------------------------------------------------------------------- the real state


class RealState:
    """What the SQLite files say, read-only and without any help from the harness."""

    def __init__(self, workdir: Path | None) -> None:
        self._db = workdir / "cortex.db" if workdir else None
        self._claims = workdir / "claims" if workdir else None

    @property
    def available(self) -> bool:
        return self._db is not None and self._db.exists()

    def _query(self, sql: str, *params: object) -> list[tuple[Any, ...]]:
        if self._db is None:
            raise RuntimeError("no world to read: check `available` first")
        with closing(sqlite3.connect(f"{self._db.resolve().as_uri()}?mode=ro", uri=True)) as db:
            return db.execute(sql, params).fetchall()

    def claims(self, tenant: str) -> tuple[int, int]:
        """(claims, distinct idempotency keys) in a company's claims file."""
        path = self._claims / f"{tenant}.db" if self._claims else None
        if path is None or not path.exists():
            return (0, 0)
        with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as db:
            row = db.execute("SELECT COUNT(*), COUNT(DISTINCT idempotency_key) FROM complaints")
            count, distinct = row.fetchone()
        return (int(count), int(distinct))

    def claims_outside(self, tenant: str) -> int:
        folder = self._claims
        if folder is None:
            return 0
        return sum(self.claims(path.stem)[0] for path in folder.glob("*.db") if path.stem != tenant)

    def sent(self, tenant: str, run_id: str) -> tuple[int, int]:
        """(replies, contacts) the run sent. A reply must answer an inbound message of the same
        conversation and run, in a run that handled no follow-up; anything else sent, however
        it is labelled, is the system contacting the customer."""
        sent = self._query(
            "SELECT conversation_id, kind, reply_to FROM outbox "
            "WHERE tenant_id = ? AND run_id = ? AND status = 'SENT'",
            tenant,
            run_id,
        )
        inbound = set(
            self._query(
                "SELECT conversation_id, message_id FROM messages "
                "WHERE tenant_id = ? AND run_id = ? AND role = 'user'",
                tenant,
                run_id,
            )
        )
        event_run = bool(
            self._query(
                "SELECT 1 FROM followup_decisions WHERE tenant_id = ? AND run_id = ?",
                tenant,
                run_id,
            )
        )
        replies = sum(
            1
            for conversation, kind, reply_to in sent
            if kind == "reply" and (conversation, reply_to) in inbound and not event_run
        )
        return replies, len(sent) - replies

    def rows_outside(self, tenant: str, run_id: str) -> int:
        total = 0
        for table in ("messages", "outbox"):
            rows = self._query(
                f"SELECT COUNT(*) FROM {table} WHERE run_id = ? AND tenant_id != ?",  # noqa: S608
                run_id,
                tenant,
            )
            total += int(rows[0][0])
        return total

    def message_count(self, tenant: str, conversation: str) -> int:
        """How many messages (of any role) the conversation holds."""
        rows = self._query(
            "SELECT COUNT(*) FROM messages WHERE tenant_id = ? AND conversation_id = ?",
            tenant,
            conversation,
        )
        return int(rows[0][0])

    def mode(self, tenant: str, conversation: str) -> str | None:
        rows = self._query(
            "SELECT mode FROM conversations WHERE tenant_id = ? AND conversation_id = ?",
            tenant,
            conversation,
        )
        return str(rows[0][0]) if rows else None

    def after_takeover(self, tenant: str, conversation: str, kind: str) -> int | None:
        """How many journal entries of `kind` came after the last takeover (None: no takeover)."""
        last = self._query(
            "SELECT MAX(seq) FROM journal "
            "WHERE tenant_id = ? AND conversation_id = ? AND kind = 'takeover'",
            tenant,
            conversation,
        )[0][0]
        if last is None:
            return None
        rows = self._query(
            "SELECT COUNT(*) FROM journal "
            "WHERE tenant_id = ? AND conversation_id = ? AND kind = ? AND seq > ?",
            tenant,
            conversation,
            kind,
            last,
        )
        return int(rows[0][0])

    def stored_trace(self, tenant: str, run_id: str) -> list[tuple[int, str]]:
        rows = self._query(
            "SELECT seq, type FROM trace_events WHERE tenant_id = ? AND run_id = ? ORDER BY seq",
            tenant,
            run_id,
        )
        return [(int(seq), str(kind)) for seq, kind in rows]


# ------------------------------------------------------------------ what `expected` may say


def _equal(name: str, wanted: object, got: object) -> Check:
    return Check(name, wanted == got, f"expected {wanted!r}, got {got!r}")


def _from_output(key: str) -> Callable[[object, Mapping[str, object]], Check]:
    return lambda wanted, output: _equal(f"expected.{key}", wanted, output.get(key))


# Every key the kit's scenarios use, and where its value is read from.
EXPECTED_RULES: Mapping[str, Callable[[object, Mapping[str, object]], Check]] = {
    key: _from_output(key)
    for key in (
        "finish_reason",
        "executed_tools",
        "rejected_tools",
        "external_calls_to_rejected_tool",
        "cross_tenant_data_exposed",
        "cross_tenant_effects",
        "contexts_separated",
        "retry_count",
        "max_steps",
        "unique_claims_created",
        "state_after_restart",
        "pending_action_retained",
        "delivered_ai_messages_after_takeover",
        "tool_effects_after_takeover",
        "decision",
        "customer_messages_sent",
        "ai_replies_delivered",
        "finish_detail",
        "claims_created",
        "step_count",
        "run_id",
        "conversation_id",
    )
}


def _stored_messages(wanted: object, output: Mapping[str, object], real: RealState) -> Check:
    name = "expected.messages_stored"
    if not real.available:
        return Check(name, False, "this key is read from the kept world, and there is none")
    got = real.message_count(str(output["tenant_id"]), str(output["conversation_id"]))
    return _equal(name, wanted, got)


# The keys that are read from the SQLite files instead of the adapter's output.
FILE_RULES: Mapping[str, Callable[[object, Mapping[str, object], RealState], Check]] = {
    "messages_stored": _stored_messages,
}


def _check_expected(
    expected: Mapping[str, object], output: Mapping[str, object], real: RealState
) -> list[Check]:
    checks = []
    for key, wanted in expected.items():
        rule = EXPECTED_RULES.get(key)
        file_rule = FILE_RULES.get(key)
        if rule is not None:
            checks.append(rule(wanted, output))
        elif file_rule is not None:
            checks.append(file_rule(wanted, output, real))
        else:
            checks.append(Check(f"expected.{key}", False, "the checker has no rule for this key"))
    return checks


# --------------------------------------------------------------------------- rules of the contract


def _contract(output: Mapping[str, object]) -> list[Check]:
    checks = []
    missing = [name for name in REQUIRED_OUTPUT if name not in output]
    checks.append(Check("contract.fields", not missing, f"missing {missing}"))
    if missing:
        return checks
    checks.append(
        Check(
            "contract.finish_reason",
            output["finish_reason"] in ALLOWED_FINISH_REASONS,
            f"not an allowed reason: {output['finish_reason']!r}",
        )
    )
    events = output["events"]
    well_formed = isinstance(events, list) and all(
        isinstance(e, dict) and e.get("type") and e.get("timestamp") for e in events
    )
    checks.append(
        Check("contract.events", bool(well_formed), "every event needs type and timestamp")
    )
    effects, state = output.get("effects"), output.get("state")
    checks.append(
        Check(
            "contract.effects_and_state",
            isinstance(effects, dict)
            and {"claims_created", "customer_messages_sent"} <= set(effects)
            and isinstance(state, dict)
            and "conversation_mode" in state,
            "effects need claims_created and customer_messages_sent; state needs conversation_mode",
        )
    )
    return checks


def _declares_a_failure(scenario: Mapping[str, object]) -> bool:
    faults = scenario.get("faults")
    if not isinstance(faults, dict):
        return False
    return any(
        str(token) not in NON_FAILING_FAULTS
        for tokens in faults.values()
        if isinstance(tokens, list)
        for token in tokens
    )


def _failure_says_why(scenario: Mapping[str, object], output: Mapping[str, object]) -> Check:
    """The contract: "En un escenario de fallo o autorizacion denegada se debe incluir evento
    tool_rejected o tool_error, con razon". A run that ends BLOCKED or FAILED, or a scenario
    that declares a failing fault, needs at least one such event with a non-empty reason."""
    raw_events = output.get("events")
    events = [e for e in raw_events if isinstance(e, dict)] if isinstance(raw_events, list) else []
    applies = output.get("finish_reason") in {"BLOCKED", "FAILED"} or _declares_a_failure(scenario)
    explained = any(
        e.get("type") in {"tool_rejected", "tool_error"} and e.get("reason") for e in events
    )
    return Check(
        "contract.a_failure_says_why_with_a_tool_event",
        not applies or explained,
        "a failure or a denial needs a tool_rejected or tool_error event with its reason",
    )


def _string_values(node: object) -> Iterator[str]:
    """Every string stored as a value anywhere in the output (never a key)."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, Mapping):
        for value in node.values():
            yield from _string_values(value)
    elif isinstance(node, list | tuple):
        for item in node:
            yield from _string_values(item)


def _rules(output: Mapping[str, object]) -> list[Check]:
    """Rules that hold for every scenario, whatever it expects."""
    raw_events = output.get("events")
    events = [e for e in raw_events if isinstance(e, dict)] if isinstance(raw_events, list) else []
    failures = [e for e in events if e["type"] in {"tool_rejected", "tool_error"}]
    denied = output.get("finish_detail") in {"auth_invalid", "scope_violation"}
    return [
        Check(
            "rule.a_denial_has_a_rejection_event",
            not denied or any(e["type"] == "tool_rejected" and e.get("reason") for e in events),
            "an authorization denial needs a tool_rejected event with its reason",
        ),
        Check(
            "rule.rejection_never_reaches_the_tool",
            output.get("external_calls_to_rejected_tool", 0) == 0,
            f"{output.get('external_calls_to_rejected_tool')} external calls to a rejected tool",
        ),
        Check(
            "rule.failures_say_why",
            all(e.get("reason") for e in failures),
            "a tool_rejected or tool_error event has no reason",
        ),
        Check(
            "rule.no_company_crossing",
            output.get("cross_tenant_effects", 0) == 0
            and output.get("cross_tenant_data_exposed") is not True,
            "data or effects crossed to another company",
        ),
        Check(
            "rule.nothing_after_a_takeover",
            not output.get("delivered_ai_messages_after_takeover")
            and not output.get("tool_effects_after_takeover"),
            "a reply or an effect happened after the takeover",
        ),
        Check(
            "rule.no_secrets_in_the_output",
            SECRET.search(json.dumps(output, ensure_ascii=False)) is None
            and not any(OPAQUE_TOKEN.search(text) for text in _string_values(output)),
            "the output contains something that looks like a secret",
        ),
    ]


# --------------------------------------------------------------------------- against the files


def _against_files(output: Mapping[str, object], real: RealState) -> list[Check]:
    """Recompute from the SQLite files what the output claims, with the checker's own queries."""
    if not real.available:
        return []
    tenant, conversation, run_id = (
        str(output[k]) for k in ("tenant_id", "conversation_id", "run_id")
    )
    claims, distinct = real.claims(tenant)
    replies, contacts = real.sent(tenant, run_id)
    effects = output.get("effects")
    effects = effects if isinstance(effects, dict) else {}
    checks = [
        *(
            _equal(f"files.{where}{key}", wanted, source.get(key))
            for key, wanted in (
                ("customer_messages_sent", contacts),
                ("ai_replies_delivered", replies),
            )
            for where, source in (("", output), ("effects.", effects))
        ),
        _equal("files.claims_created", claims, output.get("claims_created")),
        _equal("files.unique_claims_created", distinct, output.get("unique_claims_created")),
        Check(
            "files.one_row_per_claim",
            claims == distinct,
            f"{claims} claims, {distinct} distinct keys",
        ),
        _equal(
            "files.cross_tenant_effects",
            real.rows_outside(tenant, run_id) + real.claims_outside(tenant),
            output.get("cross_tenant_effects"),
        ),
    ]
    state = output.get("state")
    if isinstance(state, dict):
        checks.append(
            _equal(
                "files.conversation_mode",
                real.mode(tenant, conversation),
                state.get("conversation_mode"),
            )
        )
    for kind, key in (
        ("delivery", "delivered_ai_messages_after_takeover"),
        ("effect", "tool_effects_after_takeover"),
    ):
        if output.get(key) is not None:
            checks.append(
                _equal(f"files.{key}", real.after_takeover(tenant, conversation, kind), output[key])
            )
    events = output["events"]
    stored = real.stored_trace(tenant, run_id)
    # A replayed or refused run is not stored again, so only compare when something was stored.
    if stored and isinstance(events, list):
        checks.append(
            _equal(
                "files.trace_matches",
                stored,
                [(e["seq"], e["type"]) for e in events if isinstance(e, dict)],
            )
        )
    return checks


# --------------------------------------------------------------------------- the entry point


def check_scenario(
    scenario: Mapping[str, object],
    output: Mapping[str, object],
    workdir: Path | None,
) -> list[Check]:
    """Every check for one scenario that ran and produced `output`."""
    expected = scenario.get("expected", {})
    if not isinstance(expected, dict):
        return [Check("scenario.expected", False, "`expected` must be an object")]
    checks = _contract(output)
    if not all(check.ok for check in checks):
        return checks  # the rest reads fields that are not there
    real = RealState(workdir)
    return [
        *checks,
        _failure_says_why(scenario, output),
        *_rules(output),
        *_against_files(output, real),
        *_check_expected(expected, output, real),
    ]


def check_refusal(
    exit_code: int, expected_exit: int, stderr: str, output_exists: bool
) -> list[Check]:
    """A scenario the harness must refuse: a clear exit code, a reason, and no output file."""
    return [
        _equal("refusal.exit_code", expected_exit, exit_code),
        Check("refusal.says_why", bool(stderr.strip()), "nothing on stderr"),
        Check("refusal.no_output", not output_exists, "an output file was written anyway"),
    ]
