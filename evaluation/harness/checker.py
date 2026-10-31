"""The independent checker: did the scenario really end the way it should have?

It does not import the harness. It reads the scenario's `expected` block, the adapter's JSON
output and, when the world was kept, the SQLite files themselves, and it recomputes the effects
with its own queries. An adapter that reported something different from what is in the files, or
broke a rule of the contract, fails here even when its `expected` values happen to match.

From the files it recomputes the messages sent, the replies, the claims, the cross-company rows
and data, the conversation mode, the counts after a takeover, the trace, how the run finished
(reason and detail, from its stored `run_finished`), the tools executed and rejected,
the retries, the steps, the follow-up decision, the pending actions kept across a restart, the
mode after it, whether interleaved conversations stayed apart, and the step limit. The one thing
it cannot recompute is `external_calls_to_rejected_tool`: it is counted at the tool boundary
and nothing stores it, so it stays a check on the adapter's own output.

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

KIT = Path(__file__).resolve().parents[2] / "kit"
KIT_CONFIG = KIT / "harness_config.json"
KIT_COMPANIES = KIT / "fixtures" / "tenants_ventas.json"


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


def read_default_max_steps(config: Path) -> int:
    """The step limit of a run that sets none, as the kit's own configuration says."""
    try:
        limit = json.loads(config.read_text(encoding="utf-8"))["defaults"]["max_steps"]
    except (OSError, ValueError, KeyError, TypeError):
        limit = None
    if type(limit) is not int:
        raise ValueError(f"{config.name} has no integer defaults.max_steps")
    return limit


def read_company_contacts(fixture: Path) -> dict[str, frozenset[tuple[str, str]]]:
    """Each company's contacts, (contact_id, name), as the kit's fixture lists them."""
    try:
        rows = json.loads(fixture.read_text(encoding="utf-8"))["contacts"]
        contacts: dict[str, frozenset[tuple[str, str]]] = {}
        for row in rows:
            tenant = str(row["tenant_id"])
            contacts[tenant] = contacts.get(tenant, frozenset()) | {
                (str(row["contact_id"]), str(row["name"]))
            }
    except (OSError, ValueError, KeyError, TypeError):
        raise ValueError(f"{fixture.name} has no list of contacts") from None
    return contacts


ALLOWED_FINISH_REASONS = read_allowed_finish_reasons(KIT_CONFIG)
DEFAULT_MAX_STEPS = read_default_max_steps(KIT_CONFIG)
COMPANY_CONTACTS = read_company_contacts(KIT_COMPANIES)
# A write that is still waiting, or was confirmed and not settled, or is held for a person.
OPEN_ACTIONS = ("PENDING", "CONFIRMED", "SUSPENDED")
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
# The fields that hold an identifier. A value of theirs that is a whole UUID is an id, not a
# secret, and is not scanned; any other value is scanned like any other string.
IDENTIFIER_FIELDS = frozenset(
    {"run_id", "conversation_id", "tenant_id", "principal_id", "operation_id"}
)
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
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

    def stored_events(self, tenant: str, run_id: str) -> list[tuple[str, dict[str, Any]]]:
        """(type, attributes) of each stored event of the run, in order."""
        rows = self._query(
            "SELECT type, attributes FROM trace_events "
            "WHERE tenant_id = ? AND run_id = ? ORDER BY seq",
            tenant,
            run_id,
        )
        events = []
        for kind, raw in rows:
            try:
                attributes = json.loads(raw)
            except (TypeError, ValueError):
                attributes = None
            events.append((str(kind), attributes if isinstance(attributes, dict) else {}))
        return events

    def decision(self, tenant: str, run_id: str) -> tuple[str, list[str]] | None:
        """(decision, reasons) stored for the run's follow-up, None when it decided nothing."""
        rows = self._query(
            "SELECT decision, reasons FROM followup_decisions WHERE tenant_id = ? AND run_id = ?",
            tenant,
            run_id,
        )
        if not rows:
            return None
        return str(rows[0][0]), [reason for reason in str(rows[0][1]).split(",") if reason]

    def open_actions(self, tenant: str, conversation: str) -> set[str]:
        """The operation ids of the conversation's writes still pending, confirmed or suspended."""
        marks = ", ".join("?" for _ in OPEN_ACTIONS)
        rows = self._query(
            "SELECT operation_id FROM pending_actions "  # noqa: S608 - the marks are fixed
            f"WHERE tenant_id = ? AND conversation_id = ? AND status IN ({marks})",
            tenant,
            conversation,
            *OPEN_ACTIONS,
        )
        return {str(row[0]) for row in rows}

    def holders_of_user_message(self, text: str) -> set[tuple[str, str]]:
        """Every (company, conversation) that stores `text` as a user message."""
        rows = self._query(
            "SELECT DISTINCT tenant_id, conversation_id FROM messages "
            "WHERE role = 'user' AND content = ?",
            text,
        )
        return {(str(tenant), str(conversation)) for tenant, conversation in rows}

    def contacts(self, tenant: str) -> frozenset[tuple[str, str]]:
        rows = self._query("SELECT contact_id, name FROM contacts WHERE tenant_id = ?", tenant)
        return frozenset((str(contact_id), str(name)) for contact_id, name in rows)


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
        "decision_reasons",
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


def _attempts(events: list[tuple[str, dict[str, Any]]], tool: str) -> int:
    """How many calls of `tool` may have reached its adapter, counted from the trace.

    Each call of a tool plays the next token of its faults, so the count is: every request of
    the tool (`tool_requested`), every retry of a read (`tool_retry`), and every further write
    attempt, the one made after a lookup that found the write not applied (`write_lookup` with
    `found` false). A request that was rejected, asked for a confirmation or answered from the
    ledger never reaches the adapter but is counted all the same: the count is an upper bound,
    so a failing token that might have fired always needs its event.
    """
    return sum(
        1
        for kind, attributes in events
        if attributes.get("tool") == tool
        and (
            kind in {"tool_requested", "tool_retry"}
            or (kind == "write_lookup" and attributes.get("found") is False)
        )
    )


def _reaches_a_failure(
    scenario: Mapping[str, object], events: list[tuple[str, dict[str, Any]]]
) -> bool:
    """Did a failing fault token fire? Only when the tool was called at least as many times as
    the position of its first failing token (see `_attempts`)."""
    faults = scenario.get("faults")
    if not isinstance(faults, dict):
        return False
    for tool, tokens in faults.items():
        if not isinstance(tokens, list):
            continue
        # In any letter case, as the adapter reads them.
        failing = [
            i for i, token in enumerate(tokens, 1) if str(token).upper() not in NON_FAILING_FAULTS
        ]
        if failing and _attempts(events, str(tool)) >= failing[0]:
            return True
    return False


def _failure_says_why(
    scenario: Mapping[str, object], output: Mapping[str, object], real: RealState
) -> Check:
    """The contract: "En un escenario de fallo o autorizacion denegada se debe incluir evento
    tool_rejected o tool_error, con razon". A run that ends BLOCKED or FAILED, or a scenario
    whose failing fault really fired, needs at least one such event with a non-empty reason.
    The calls are counted from the stored trace, or from the output's when none was stored."""
    raw_events = output.get("events")
    events = [e for e in raw_events if isinstance(e, dict)] if isinstance(raw_events, list) else []
    stored = (
        real.stored_events(str(output["tenant_id"]), str(output["run_id"]))
        if real.available
        else []
    )
    trace = stored or [(str(e.get("type")), e) for e in events]
    applies = output.get("finish_reason") in {"BLOCKED", "FAILED"} or _reaches_a_failure(
        scenario, trace
    )
    explained = any(
        e.get("type") in {"tool_rejected", "tool_error"} and e.get("reason") for e in events
    )
    return Check(
        "contract.a_failure_says_why_with_a_tool_event",
        not applies or explained,
        "a failure or a denial needs a tool_rejected or tool_error event with its reason",
    )


def _string_values(node: object) -> Iterator[str]:
    """Every string stored as a value anywhere in the output (never a key), except a whole UUID
    in an identifier field: an id the caller chose (a UUID run_id, say) is not a secret, but an
    operation_id the model chose may be one. A number is never one either (a control version of
    2147483647)."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, Mapping):
        for key, value in node.items():
            if key not in IDENTIFIER_FIELDS:
                yield from _string_values(value)
            elif isinstance(value, str) and not UUID.fullmatch(value):
                yield value
    elif isinstance(node, list | tuple):
        for item in node:
            yield from _string_values(item)


def _keys(node: object) -> Iterator[str]:
    """Every key anywhere in the output."""
    if isinstance(node, Mapping):
        for key, value in node.items():
            yield str(key)
            yield from _keys(value)
    elif isinstance(node, list | tuple):
        for item in node:
            yield from _keys(item)


def _rules(output: Mapping[str, object]) -> list[Check]:
    """Rules that hold for every scenario, whatever it expects."""
    raw_events = output.get("events")
    events = [e for e in raw_events if isinstance(e, dict)] if isinstance(raw_events, list) else []
    failures = [e for e in events if e["type"] in {"tool_rejected", "tool_error"}]
    denied = output.get("finish_detail") in {"auth_invalid", "scope_violation"}
    rejection = any(e["type"] == "tool_rejected" and e.get("reason") for e in events)
    raw_chat = output.get("chat_events")
    chat_refused = isinstance(raw_chat, list) and any(
        isinstance(e, dict) and e.get("type") == "chat_event_rejected" for e in raw_chat
    )
    return [
        Check(
            "rule.a_denial_has_a_rejection_event",
            not denied or rejection,
            "an authorization denial needs a tool_rejected event with its reason",
        ),
        Check(
            "rule.a_refused_chat_event_has_a_rejection_event",
            not chat_refused or rejection,
            "a refused chat event is a denial: it needs a tool_rejected event with its reason",
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
            not any(SECRET.search(text) for text in (*_string_values(output), *_keys(output)))
            and not any(OPAQUE_TOKEN.search(text) for text in _string_values(output)),
            "the output contains something that looks like a secret",
        ),
    ]


# --------------------------------------------------------------------------- against the files


def _tools_named(events: list[tuple[str, dict[str, Any]]], kind: str) -> list[str]:
    """The tool of each stored event of a type. An event about the run itself names no tool."""
    return [
        str(attributes["tool"])
        for event, attributes in events
        if event == kind and attributes.get("tool") is not None
    ]


def _preconditions(scenario: Mapping[str, object]) -> Mapping[str, object]:
    preconditions = scenario.get("preconditions")
    return preconditions if isinstance(preconditions, dict) else {}


def _step_limit(preconditions: Mapping[str, object]) -> int:
    """The scenario's own step limit, otherwise the kit's default."""
    limit = preconditions.get("max_steps")
    return limit if type(limit) is int else DEFAULT_MAX_STEPS


def _contexts_separated(preconditions: Mapping[str, object], real: RealState) -> bool | None:
    """Did the interleaved conversations stay apart? None when the scenario has none, or when
    nothing was stored (the run was refused before anything ran).

    Every interleaved message must be stored as a user message under its own company and
    conversation and under no other, and each company's contacts must be exactly its own. A
    message stored nowhere was refused before it was stored (too long, say): it crossed nowhere,
    so it is left out."""
    entries = preconditions.get("interleaved_conversations")
    if not isinstance(entries, list):
        return None
    where: dict[str, set[tuple[str, str]]] = {}
    tenants = set()
    for entry in entries:
        if isinstance(entry, dict):
            where.setdefault(str(entry.get("message")), set()).add(
                (str(entry.get("tenant_id")), str(entry.get("conversation_id")))
            )
            tenants.add(str(entry.get("tenant_id")))
    held = {text: real.holders_of_user_message(text) for text in where}
    if not any(held.values()):
        return None
    return all(held[text] == where[text] for text in where if held[text]) and all(
        real.contacts(tenant) == COMPANY_CONTACTS.get(tenant, frozenset()) for tenant in tenants
    )


def _after_restart(
    preconditions: Mapping[str, object], real: RealState, tenant: str, conversation: str
) -> tuple[str | None, bool | None]:
    """(mode, pending writes kept) of a scenario that restarts the process; (None, None) when it
    does not, or when the conversation was never stored (the run was refused)."""
    mode = real.mode(tenant, conversation)
    if preconditions.get("restart_between_events") is not True or mode is None:
        return None, None
    pending = preconditions.get("pending_action")
    wanted = {str(pending.get("operation_id"))} if isinstance(pending, dict) else set()
    return mode, wanted <= real.open_actions(tenant, conversation)


def _against_files(
    scenario: Mapping[str, object], output: Mapping[str, object], real: RealState
) -> list[Check]:
    """Recompute from the SQLite files what the output claims, with the checker's own queries.
    Only `external_calls_to_rejected_tool` is left to the output: it is counted at the tool
    boundary and nothing stores it."""
    if not real.available:
        return []
    tenant, conversation, run_id = (
        str(output[k]) for k in ("tenant_id", "conversation_id", "run_id")
    )
    preconditions = _preconditions(scenario)
    claims, distinct = real.claims(tenant)
    replies, contacts = real.sent(tenant, run_id)
    effects = output.get("effects")
    effects = effects if isinstance(effects, dict) else {}
    stored_events = real.stored_events(tenant, run_id)
    decision = real.decision(tenant, run_id)
    mode_after_restart, pending_kept = _after_restart(preconditions, real, tenant, conversation)
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
        _equal(
            "files.cross_tenant_data_exposed",
            _foreign_data(stored_events, str(scenario.get("tenant_id", tenant))),
            output.get("cross_tenant_data_exposed"),
        ),
        _equal(
            "files.executed_tools",
            _tools_named(stored_events, "tool_executed"),
            output.get("executed_tools"),
        ),
        _equal(
            "files.rejected_tools",
            _tools_named(stored_events, "tool_rejected"),
            output.get("rejected_tools"),
        ),
        _equal(
            "files.retry_count",
            sum(1 for kind, _ in stored_events if kind == "tool_retry"),
            output.get("retry_count"),
        ),
        _equal(
            "files.step_count",
            sum(1 for kind, _ in stored_events if kind == "model_called"),
            output.get("step_count"),
        ),
        _equal("files.max_steps", _step_limit(preconditions), output.get("max_steps")),
        _equal("files.decision", decision[0] if decision else None, output.get("decision")),
        _equal(
            "files.decision_reasons",
            decision[1] if decision else [],
            output.get("decision_reasons"),
        ),
        _equal("files.state_after_restart", mode_after_restart, output.get("state_after_restart")),
        _equal(
            "files.pending_action_retained", pending_kept, output.get("pending_action_retained")
        ),
        _equal(
            "files.contexts_separated",
            _contexts_separated(preconditions, real),
            output.get("contexts_separated"),
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
    finished = [attributes for kind, attributes in stored_events if kind == "run_finished"]
    if finished:
        checks += [
            _equal(f"files.{key}", finished[-1].get(key), output.get(key))
            for key in ("finish_reason", "finish_detail")
        ]
    return checks


def _foreign_data(events: list[tuple[str, dict[str, Any]]], tenant: str) -> bool:
    """Did any stored event tagged with `data_tenant` carry data of a company other than the
    scenario's own? Not only `tool_executed`: errors, deduplicated and reconciled results carry
    the tag too."""
    return any(attributes.get("data_tenant") not in (None, tenant) for _, attributes in events)


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
        _failure_says_why(scenario, output, real),
        *_rules(output),
        *_against_files(scenario, output, real),
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
