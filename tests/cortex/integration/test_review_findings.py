"""Regression tests for what the adversarial reviews (two blind judges) found.

Each test is named after its finding. Every one failed, or could not even be written, before the
fix: the critical one of the first review (commit abb4006) let an uncertain write run again
without a valid confirmation. The section "final review" holds the findings of the last one.
"""

import hashlib
import json
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from cortex.bootstrap import BUSINESS_TIMEZONE, DEFAULT_TRACE_KEY, HarnessData, build_runner
from cortex.chat.continuity import continuity_card
from cortex.chat.events import FollowupFacts
from cortex.chat.service import ChatEvents, RunIdConflict
from cortex.kit_adapter.audit import Audit
from cortex.kit_adapter.orchestrator import run_scenario_file
from cortex.kit_adapter.scenario import UnsupportedScenario
from cortex.memory.database import Database
from cortex.memory.ledger import OperationStatus, PendingStatus
from cortex.memory.tenant_store import Deliveries
from cortex.observability.redaction import TraceRedactor
from cortex.runner import agent_runner
from cortex.runner.agent_runner import RunRequest, RunResult
from cortex.runner.finish import FinishDetail
from cortex.runner.model import ModelTurn, Prompt
from cortex.runner.scripted_model import ScriptedModel
from cortex.timekeeping import ManualTicker
from cortex.tools.adapters.claims import ClaimsDesk
from cortex.tools.failures import UncertainOutcome
from cortex.tools.gateway import WriteAdapter
from cortex.tools.policy import review
from merchant_assistant.agent.domain.messages import ToolCall
from merchant_assistant.control.states import State
from merchant_assistant.shared.clock import FixedClock

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[3]
CLOCK = FixedClock(datetime(2026, 10, 28, 12, tzinfo=UTC))
CLAIM = {"motivo": "demora", "descripcion": "No recibi comprobante", "operation_id": "op_claim_001"}
ASK = {"tool": "crear_reclamo", "args": CLAIM}


class _Flaky:
    """The claims system: `failures` calls fail uncertainly (after landing if `commit_first`)."""

    def __init__(
        self, real: WriteAdapter, *, failures: int = 0, commit_first: bool = False
    ) -> None:
        self.real, self.failures, self.commit_first = real, failures, commit_first
        self.tenant_id = real.tenant_id
        self.lookup_fails = False
        self.raise_unexpected = False
        self.creates = 0

    def __call__(self, arguments: BaseModel, *, conversation_id: str) -> Mapping[str, object]:
        self.creates += 1
        if self.raise_unexpected:
            raise RuntimeError("a bug in the adapter")
        if self.failures > 0:
            self.failures -= 1
            if self.commit_first:
                self.real(arguments, conversation_id=conversation_id)
            raise UncertainOutcome("the answer was lost")
        return self.real(arguments, conversation_id=conversation_id)

    def lookup(self, operation_id: str) -> Mapping[str, object] | None:
        if self.lookup_fails:
            raise UncertainOutcome("the claims system does not answer")
        return self.real.lookup(operation_id)


class _TakeoverAfterToolRequest:
    """A model whose tool request is followed, before the harness acts, by a takeover."""

    def __init__(self, script: list[dict[str, Any]], take_over: Callable[[], None]) -> None:
        self.model, self.take_over, self.done = ScriptedModel(script), take_over, False

    def next_turn(self, prompt: Prompt, *, timeout_ms: int) -> ModelTurn:
        turn = self.model.next_turn(prompt, timeout_ms=timeout_ms)
        if isinstance(turn, ToolCall) and not self.done:
            self.done = True
            self.take_over()
        return turn


class _Rig:
    def __init__(
        self, workdir: Path, data: HarnessData, *, failures: int = 0, commit_first: bool = False
    ) -> None:
        self.workdir, self.data = workdir, data
        self.auth = data.directory.authenticate("comercio_a", "operador_a")
        self.database = Database(workdir / "cortex.db", CLOCK)
        self.database.initialize()
        real = ClaimsDesk(workdir / "claims", CLOCK).for_tenant(self.auth)
        self.flaky = _Flaky(real, failures=failures, commit_first=commit_first)

    @property
    def store(self) -> Any:
        return self.database.for_tenant(self.auth)

    def run(
        self,
        message: str,
        script: list[dict[str, Any]],
        run_id: str,
        *,
        conversation: str = "conv_1",
        model: Any = None,
        tools: frozenset[str] | None = None,
    ) -> RunResult:
        runner = build_runner(
            data=self.data,
            database=self.database,
            model=model or ScriptedModel(script),
            bind_tools=lambda auth: {"crear_reclamo": self.flaky},
            clock=CLOCK,
            ticker=ManualTicker(),
        )
        return runner.run(
            RunRequest(
                run_id, conversation, message, self.auth, self.data.config.limits, tools=tools
            )
        )

    def takeover(self, version: int, conversation: str = "conv_1") -> None:
        assert self.store.chat.apply_takeover(conversation, version, "operador_a").applied

    def hand_back(self, version: int, conversation: str = "conv_1") -> None:
        self.store.chat.set_control(conversation, State.AUTO, version)

    @property
    def claims(self) -> int:
        return Audit(self.database.path, ClaimsDesk(self.workdir / "claims", CLOCK)).claims(
            "comercio_a"
        )[0]

    def types(self, result: RunResult) -> list[str]:
        return [event.type for event in result.events]


@pytest.fixture
def make_rig(tmp_path: Path, harness_data: HarnessData) -> Callable[..., _Rig]:
    return lambda **kwargs: _Rig(tmp_path, harness_data, **kwargs)


def _uncertain_then_takeover(rig: _Rig) -> None:
    """A confirmed write whose outcome stays unknown, then an operator takes over and gives the
    conversation back: the original confirmation is suspended, not valid any more."""
    rig.run("Quiero reclamar", [ASK], "run-1")
    unresolved = rig.run("sí", [{"final": "?"}], "run-2")
    assert unresolved.finish_detail is FinishDetail.WRITE_OUTCOME_UNKNOWN
    rig.takeover(version=1)
    rig.hand_back(version=2)


# --------------------------------------------------------------------------- C1


def test_c1_an_uncertain_write_that_did_not_land_needs_a_new_confirmation(
    make_rig: Callable[..., _Rig],
) -> None:
    rig = make_rig(failures=2)
    _uncertain_then_takeover(rig)

    again = rig.run("Repetilo", [ASK, {"final": "?"}], "run-3")

    assert again.finish_detail is FinishDetail.AWAITING_CONFIRMATION
    assert rig.claims == 0  # it was looked up, not re-sent

    confirmed = rig.run("sí", [{"final": "Registrado"}], "run-4")
    assert "confirmation_received" in rig.types(confirmed)
    assert rig.claims == 1


def test_c1_an_uncertain_write_that_did_land_is_recorded_without_asking_and_closed(
    make_rig: Callable[..., _Rig],
) -> None:
    rig = make_rig(failures=1, commit_first=True)
    rig.flaky.lookup_fails = True  # the first time, nobody can tell whether it landed
    _uncertain_then_takeover(rig)
    rig.flaky.lookup_fails = False

    result = rig.run("¿Se registró?", [ASK, {"final": "Sí"}], "run-3")

    assert "write_reconciled" in rig.types(result)
    assert rig.flaky.creates == 1
    pending = rig.store.ledger.pending_action("op_claim_001")
    assert pending.status is PendingStatus.CONSUMED  # was SUSPENDED: no longer shown as pending


def test_c1_another_conversation_cannot_run_an_uncertain_command(
    make_rig: Callable[..., _Rig],
) -> None:
    rig = make_rig(failures=2)
    rig.run("Quiero reclamar", [ASK], "run-1")
    rig.run("sí", [{"final": "?"}], "run-2")

    other = rig.run("Yo también", [ASK, {"final": "No"}], "run-3", conversation="conv_2")

    rejection = next(e for e in other.events if e.type == "tool_rejected")
    assert rejection.attributes["reason"] == "operation_id_conflict"
    assert rig.claims == 0


# --------------------------------------------------------------------------- W1


def _takeover_right_after_verification(monkeypatch: pytest.MonkeyPatch, rig: _Rig) -> None:
    """The window the judges found: an operator takes over after the policy approved the request
    and after checkpoint B, before the write's own transaction. Checkpoint B is disabled here so
    that only the write's atomic checks stand between the takeover and the claims system."""
    original = review

    def review_then_takeover(*args: Any, **kwargs: Any) -> Any:
        verdict = original(*args, **kwargs)
        rig.takeover(version=1)
        return verdict

    monkeypatch.setattr(agent_runner, "review", review_then_takeover)
    monkeypatch.setattr(agent_runner._Run, "_control_changed", lambda self: False)


def test_w1_a_takeover_between_the_checkpoint_and_the_claim_stops_the_write(
    make_rig: Callable[..., _Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make_rig()
    rig.run("Quiero reclamar", [ASK], "run-0")  # the proposal, with its exact values
    assert rig.store.ledger.confirm_pending("op_claim_001", "conv_1", 0)  # the user said yes
    _takeover_right_after_verification(monkeypatch, rig)

    result = rig.run("Registralo", [ASK, {"final": "Listo"}], "run-1")

    assert result.finish_detail is FinishDetail.OPERATOR_TOOK_OVER
    assert rig.flaky.creates == 0
    assert rig.claims == 0


def test_w1_a_question_after_a_takeover_is_not_left_pending(
    make_rig: Callable[..., _Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make_rig()
    _takeover_right_after_verification(monkeypatch, rig)

    result = rig.run("Quiero reclamar", [ASK], "run-1")

    assert result.finish_detail is FinishDetail.OPERATOR_TOOK_OVER
    assert (
        rig.store.ledger.pending_action("op_claim_001") is None
    )  # nothing a later "sí" could confirm


def test_w1_a_yes_never_confirms_a_question_asked_under_another_control_version(
    make_rig: Callable[..., _Rig],
) -> None:
    rig = make_rig()
    rig.run("Quiero reclamar", [ASK], "run-1")  # asked at version 0
    rig.hand_back(version=1)  # the version moved without a takeover event

    rig.run("sí", [{"final": "?"}], "run-2")

    assert rig.store.ledger.pending_action("op_claim_001").status is PendingStatus.CANCELLED
    assert rig.claims == 0


# --------------------------------------------------------------------------- W2


def test_w2_an_unexpected_error_after_the_claim_leaves_the_command_uncertain_not_stuck(
    make_rig: Callable[..., _Rig],
) -> None:
    rig = make_rig()
    rig.run("Quiero reclamar", [ASK], "run-1")
    rig.flaky.raise_unexpected = True

    crashed = rig.run("sí", [{"final": "?"}], "run-2")
    operation = rig.store.ledger.operation("op_claim_001")

    assert crashed.finish_detail is FinishDetail.INTERNAL_ERROR
    assert operation.status is OperationStatus.UNCERTAIN
    rig.flaky.raise_unexpected = False
    asked = rig.run("Repetilo", [ASK, {"final": "Listo"}], "run-3")
    # The attempt used the confirmation: a message that is not a "sí" does not renew it.
    assert asked.finish_detail is FinishDetail.AWAITING_CONFIRMATION
    assert rig.claims == 0
    retried = rig.run("sí", [{"final": "Listo"}], "run-4")
    assert "tool_executed" in rig.types(retried)  # looked up, not found, confirmed again: runs
    assert rig.claims == 1


def test_w2_a_command_whose_run_ended_without_settling_it_is_reconciled(
    make_rig: Callable[..., _Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make_rig()
    rig.run("Quiero reclamar", [ASK], "run-1")
    original = type(rig.store.ledger).settle_operation

    def broken(
        self: Any, operation_id: str, status: OperationStatus, *args: Any, **kwargs: Any
    ) -> None:
        if status is OperationStatus.COMMITTED:
            raise sqlite3.OperationalError("database is locked")
        original(self, operation_id, status, *args, **kwargs)

    monkeypatch.setattr(type(rig.store.ledger), "settle_operation", broken)
    rig.run("sí", [{"final": "?"}], "run-2")  # the claim lands, its record does not
    monkeypatch.setattr(type(rig.store.ledger), "settle_operation", original)
    assert rig.store.ledger.operation("op_claim_001").status is OperationStatus.STARTED

    later = rig.run("¿Se registró?", [ASK, {"final": "Sí"}], "run-3")

    assert "write_reconciled" in rig.types(later)
    assert rig.claims == 1


# --------------------------------------------------------------------------- W3


def test_w3_a_run_interrupted_by_a_crash_is_not_run_again_under_the_same_id(
    make_rig: Callable[..., _Rig],
) -> None:
    rig = make_rig()
    rig.store.open_conversation("conv_1")
    digest = hashlib.sha256(b"Hola").hexdigest()  # the same message the run will send again
    assert rig.store.runs.admit("run-9", "conv_1", digest).admission == "ADMITTED"
    rig.database.recover()  # the process died while run-9 was running
    before = len(rig.store.recent_messages("conv_1", 50))

    result = rig.run("Hola", [{"final": "Hola"}], "run-9")

    assert result.finish_detail is FinishDetail.RUN_INTERRUPTED
    assert len(rig.store.recent_messages("conv_1", 50)) == before
    assert rig.store.deliveries("run-9") == Deliveries(0, 0)


# --------------------------------------------------------------------------- W4


def test_w4_the_card_shows_only_this_conversations_uncertain_commands_masked(
    make_rig: Callable[..., _Rig],
) -> None:
    rig = make_rig(failures=2)
    secret_id = {**CLAIM, "operation_id": "op_4111111111111111"}
    rig.run(
        "Quiero reclamar",
        [{"tool": "crear_reclamo", "args": secret_id}],
        "run-1",
        conversation="conv_2",
    )
    rig.run("sí", [{"final": "?"}], "run-2", conversation="conv_2")
    rig.run("Hola", [{"final": "Hola"}], "run-3")

    mine = continuity_card(rig.store, "conv_1")
    theirs = continuity_card(rig.store, "conv_2")

    assert mine["uncertain_operations"] == []
    assert theirs["uncertain_operations"] == ["op_[redacted]"]


# --------------------------------------------------------------------------- W5


@pytest.mark.parametrize("bad", ["a b", "conv/../x", "x" * 129])
def test_w5_an_invalid_conversation_id_is_an_unsupported_scenario(tmp_path: Path, bad: str) -> None:
    data = json.loads((ROOT / "examples" / "T01.json").read_text())
    data["conversation_id"] = bad
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(data))

    with pytest.raises(UnsupportedScenario):
        run_scenario_file(path, kit_dir=ROOT / "kit")

    done = subprocess.run(  # noqa: S603 - the project's own entry point
        [
            sys.executable,
            str(ROOT / "evaluation_adapter.py"),
            "--scenario",
            str(path),
            "--output",
            str(tmp_path / "out.json"),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert done.returncode == 2
    assert not (tmp_path / "out.json").exists()


# --------------------------------------------------------------------------- suspects


def test_the_confirmation_question_marks_where_each_value_starts_and_ends(
    make_rig: Callable[..., _Rig],
) -> None:
    rig = make_rig()
    rig.run("Quiero reclamar", [ASK], "run-1")

    question = rig.store.last_outbox("conv_1", "SENT")

    assert "- descripcion: «No recibi comprobante»" in question
    assert "- operation_id: «op_claim_001»" in question


def test_the_same_follow_up_handled_twice_is_decided_once(make_rig: Callable[..., _Rig]) -> None:
    rig = make_rig()
    chat = ChatEvents(
        rig.database,
        rig.data.directory,
        CLOCK,
        BUSINESS_TIMEZONE,
        TraceRedactor(DEFAULT_TRACE_KEY),
    )
    facts = FollowupFacts(pending_on="ZOCO", responsible="Benjamin", contact_allowed=True)

    first = chat.follow_up(
        rig.auth, "conv_1", "run-f", facts, chat.tracer(rig.auth, "conv_1", "run-f")
    )
    replayed_tracer = chat.tracer(rig.auth, "conv_1", "run-f")
    again = chat.follow_up(rig.auth, "conv_1", "run-f", facts, replayed_tracer)

    assert again.decision == first.decision
    assert [e.type for e in replayed_tracer.events] == ["followup_replayed"]
    with sqlite3.connect(rig.database.path) as db:
        assert db.execute("SELECT COUNT(*) FROM followup_decisions").fetchone()[0] == 1


# =========================================================================== second pass
#
# What the second adversarial pass found (no critical defect, five minor ones).


def _scenario_file(tmp_path: Path, example: str, **changes: Any) -> Path:
    data = json.loads((ROOT / "examples" / example).read_text())
    data.update(changes)
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(data))
    return path


def _adapter_exit_code(path: Path, out: Path) -> int:
    done = subprocess.run(  # noqa: S603 - the project's own entry point
        [
            sys.executable,
            str(ROOT / "evaluation_adapter.py"),
            "--scenario",
            str(path),
            "--output",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert "Traceback" not in done.stderr
    return done.returncode


# --------------------------------------------------------------------------- run ids


def test_a_conversation_id_whose_run_id_is_too_long_is_an_unsupported_scenario(
    tmp_path: Path,
) -> None:
    long_id = "c" * 125  # a valid conversation id, but "run-" + it is 129 characters
    path = _scenario_file(tmp_path, "T01.json", conversation_id=long_id)

    with pytest.raises(UnsupportedScenario):
        run_scenario_file(path, kit_dir=ROOT / "kit")

    assert _adapter_exit_code(path, tmp_path / "out.json") == 2
    assert not (tmp_path / "out.json").exists()


def test_an_interleaved_conversation_whose_run_id_is_too_long_is_an_unsupported_scenario(
    tmp_path: Path,
) -> None:
    data = json.loads((ROOT / "examples" / "T06.json").read_text())
    interleaved = data["preconditions"]["interleaved_conversations"]
    interleaved[0]["conversation_id"] = "c" * 123  # "run-" + it + "-1" is 129 characters
    path = _scenario_file(tmp_path, "T06.json", preconditions=data["preconditions"])

    with pytest.raises(UnsupportedScenario):
        run_scenario_file(path, kit_dir=ROOT / "kit")

    assert _adapter_exit_code(path, tmp_path / "out.json") == 2


# --------------------------------------------------------------------------- ownership


def _operation_effects(rig: _Rig) -> list[tuple[str, str]]:
    with sqlite3.connect(rig.database.path) as db:
        rows = db.execute(
            "SELECT conversation_id, reference FROM journal WHERE kind = 'effect'"
        ).fetchall()
    return [(str(conversation), str(reference)) for conversation, reference in rows]


def test_another_conversation_cannot_reconcile_an_uncertain_command_that_did_land(
    make_rig: Callable[..., _Rig],
) -> None:
    rig = make_rig(failures=1, commit_first=True)
    rig.flaky.lookup_fails = True  # nobody can tell whether the claim landed
    rig.run("Quiero reclamar", [ASK], "run-1")
    unresolved = rig.run("sí", [{"final": "?"}], "run-2")
    assert unresolved.finish_detail is FinishDetail.WRITE_OUTCOME_UNKNOWN
    rig.flaky.lookup_fails = False  # the claim did land, and now it could be found

    other = rig.run("Yo también", [ASK, {"final": "No"}], "run-3", conversation="conv_2")

    rejection = next(e for e in other.events if e.type == "tool_rejected")
    assert rejection.attributes["reason"] == "operation_id_conflict"
    assert "write_reconciled" not in rig.types(other)
    assert _operation_effects(rig) == []  # nothing journaled, least of all in conv_2
    assert rig.store.ledger.pending_action("op_claim_001").status is PendingStatus.CONFIRMED
    assert rig.store.ledger.operation("op_claim_001").status is OperationStatus.UNCERTAIN

    # The owner can still reconcile it, and the effect is journaled in its conversation.
    owner = rig.run("¿Se registró?", [ASK, {"final": "Sí"}], "run-4")
    assert "write_reconciled" in rig.types(owner)
    assert _operation_effects(rig) == [("conv_1", "op_claim_001")]


# --------------------------------------------------------------------------- spent confirmation


@pytest.mark.parametrize("message", ["no, dejalo", "sí"])
def test_a_confirmation_spent_on_an_uncertain_write_is_never_reused(
    make_rig: Callable[..., _Rig], message: str
) -> None:
    # Even a "sí" nobody asked for: one confirmation is good for one attempt.
    rig = make_rig(failures=2)
    rig.run("Quiero reclamar", [ASK], "run-1")
    unresolved = rig.run("sí", [{"final": "?"}], "run-2")
    assert unresolved.finish_detail is FinishDetail.WRITE_OUTCOME_UNKNOWN
    calls_before = rig.flaky.creates

    again = rig.run(message, [ASK, {"final": "?"}], "run-3")

    assert again.finish_detail is FinishDetail.AWAITING_CONFIRMATION
    assert "confirmation_cancelled" in rig.types(again)
    assert rig.flaky.creates == calls_before  # the adapter was not called
    assert rig.claims == 0
    assert rig.store.ledger.pending_action("op_claim_001").status is PendingStatus.PENDING

    confirmed = rig.run("sí", [{"final": "Registrado"}], "run-4")  # a new question, a new yes
    assert "tool_executed" in rig.types(confirmed)
    assert rig.claims == 1


# --------------------------------------------------------------------------- follow-up replay


def _chat(rig: _Rig) -> ChatEvents:
    return ChatEvents(
        rig.database,
        rig.data.directory,
        CLOCK,
        BUSINESS_TIMEZONE,
        TraceRedactor(DEFAULT_TRACE_KEY),
    )


def _decisions(rig: _Rig) -> int:
    with sqlite3.connect(rig.database.path) as db:
        return int(db.execute("SELECT COUNT(*) FROM followup_decisions").fetchone()[0])


FACTS = FollowupFacts(pending_on="ZOCO", responsible="Benjamin", contact_allowed=True)


def test_a_follow_up_run_id_reused_for_another_conversation_is_a_conflict(
    make_rig: Callable[..., _Rig],
) -> None:
    rig = make_rig()
    chat = _chat(rig)
    chat.follow_up(rig.auth, "conv_1", "run-f", FACTS, chat.tracer(rig.auth, "conv_1", "run-f"))

    with pytest.raises(RunIdConflict):
        chat.follow_up(rig.auth, "conv_2", "run-f", FACTS, chat.tracer(rig.auth, "conv_2", "run-f"))

    assert _decisions(rig) == 1
    assert rig.store.chat.open_commitments("conv_2") == ()


def test_a_follow_up_run_id_reused_with_other_facts_is_a_conflict(
    make_rig: Callable[..., _Rig],
) -> None:
    rig = make_rig()
    chat = _chat(rig)
    chat.follow_up(rig.auth, "conv_1", "run-f", FACTS, chat.tracer(rig.auth, "conv_1", "run-f"))
    other_facts = FollowupFacts(pending_on="CUSTOMER", contact_allowed=True)

    with pytest.raises(RunIdConflict):
        chat.follow_up(
            rig.auth, "conv_1", "run-f", other_facts, chat.tracer(rig.auth, "conv_1", "run-f")
        )

    assert _decisions(rig) == 1


@pytest.mark.parametrize("same_event", [True, False])
def test_a_follow_up_decided_concurrently_is_a_replay_or_a_conflict_never_an_error(
    make_rig: Callable[..., _Rig], monkeypatch: pytest.MonkeyPatch, same_event: bool
) -> None:
    rig = make_rig()
    chat = _chat(rig)
    records = type(rig.store.chat)
    original_read, original_record = records.decision_of, records.record_followup
    state = {"raced": False}

    def read_then_lose_the_race(self: Any, run_id: str) -> Any:
        found = original_read(self, run_id)
        if not state["raced"]:
            state["raced"] = True  # the competitor commits between our read and our insert
            winner = FACTS if same_event else FollowupFacts(pending_on="CUSTOMER")
            original_record(
                self,
                "conv_1",
                run_id,
                winner.digest(),
                "TAREA_INTERNA",
                ("ZOCO_OWES_ACTION",),
                None,
            )
        return found

    monkeypatch.setattr(records, "decision_of", read_then_lose_the_race)
    tracer = chat.tracer(rig.auth, "conv_1", "run-f")

    if same_event:
        replayed = chat.follow_up(rig.auth, "conv_1", "run-f", FACTS, tracer)
        assert replayed.decision.value == "TAREA_INTERNA"
        assert [e.type for e in tracer.events] == ["followup_replayed"]
    else:
        with pytest.raises(RunIdConflict):
            chat.follow_up(rig.auth, "conv_1", "run-f", FACTS, tracer)
    assert _decisions(rig) == 1


# --------------------------------------------------------------------------- question escaping


def test_a_value_cannot_fake_another_field_in_the_confirmation_question(
    make_rig: Callable[..., _Rig],
) -> None:
    rig = make_rig()
    forged = {**CLAIM, "descripcion": "ok» - monto: «999 y \\ también «"}

    rig.run("Quiero reclamar", [{"tool": "crear_reclamo", "args": forged}], "run-1")
    question = rig.store.last_outbox("conv_1", "SENT")

    fields = [line for line in question.splitlines() if line.startswith("- ")]
    assert len(fields) == 3
    assert "- descripcion: «ok\\» - monto: \\«999 y \\\\ también \\«»" in question
    # The confirmation still binds the raw values: the same call is confirmed by one "sí".
    assert rig.store.ledger.pending_action("op_claim_001").fingerprint is not None
    rig.run("sí", [{"final": "Listo"}], "run-2")
    assert rig.claims == 1


# --------------------------------------------------------------------------- final review


def test_f1_a_yes_whose_write_is_rejected_before_any_attempt_does_not_stay_confirmed(
    make_rig: Callable[..., _Rig],
) -> None:
    rig = make_rig()
    rig.run("Quiero reclamar", [ASK], "run-1")

    # This run may not use the claims tool: the confirmed write is rejected before any attempt.
    refused = rig.run("sí", [{"final": "No puedo"}], "run-2", tools=frozenset())

    rejection = next(e for e in refused.events if e.type == "tool_rejected")
    assert rejection.attributes["reason"] == "tool_not_allowed"
    assert "confirmation_cancelled" in rig.types(refused)
    assert rig.store.ledger.pending_action("op_claim_001").status is PendingStatus.CANCELLED

    # A later run with every tool asks again for the same values instead of running them.
    again = rig.run("Quiero reclamar", [ASK, {"final": "?"}], "run-3")

    assert again.finish_detail is FinishDetail.AWAITING_CONFIRMATION
    assert rig.flaky.creates == 0
    assert rig.claims == 0


def _decide(rig: _Rig, run_id: str, **facts: Any) -> None:
    chat = _chat(rig)
    tracer = chat.tracer(rig.auth, "conv_1", run_id)
    chat.follow_up(rig.auth, "conv_1", run_id, FollowupFacts(**facts), tracer)


def _owed(rig: _Rig) -> list[dict[str, object]]:
    commitments = continuity_card(rig.store, "conv_1")["commitments"]
    assert isinstance(commitments, list)
    return commitments


TASK_FOR_A = {"opportunity_id": "opp_a", "pending_on": "ZOCO", "responsible": "Benjamin"}
OWED_ON_A = {
    "kind": "internal_task",
    "responsible": "Benjamin",
    "due_date": None,
    "opportunity_id": "opp_a",
}


def test_f3_cancelling_one_opportunity_keeps_the_task_owed_on_another(
    make_rig: Callable[..., _Rig],
) -> None:
    rig = make_rig()
    _decide(rig, "run-a", **TASK_FOR_A)

    _decide(rig, "run-b", opportunity_id="opp_b", opted_out=True)

    assert _owed(rig) == [OWED_ON_A]


def test_f3_cancelling_the_same_opportunity_cancels_its_task(
    make_rig: Callable[..., _Rig],
) -> None:
    rig = make_rig()
    _decide(rig, "run-a", **TASK_FOR_A)

    _decide(rig, "run-a2", opportunity_id="opp_a", opted_out=True)

    assert _owed(rig) == []


def test_f3_decisions_without_an_opportunity_only_replace_each_other(
    make_rig: Callable[..., _Rig],
) -> None:
    rig = make_rig()
    _decide(rig, "run-1", pending_on="ZOCO", responsible="Benjamin")
    _decide(rig, "run-a", **TASK_FOR_A)
    assert [c["opportunity_id"] for c in _owed(rig)] == [None, "opp_a"]

    _decide(rig, "run-2", opted_out=True)  # no opportunity: what is owed without one

    assert _owed(rig) == [OWED_ON_A]


def test_f6_the_responsible_of_a_commitment_is_scrubbed_and_capped(
    make_rig: Callable[..., _Rig],
) -> None:
    rig = make_rig()
    _decide(rig, "run-f", pending_on="ZOCO", responsible="ana.perez@example.com " + "x" * 500)
    model = ScriptedModel([{"final": "OK"}])

    rig.run("Hola", [], "run-1", model=model)

    for commitment in (_owed(rig)[0], model.prompts[0].state["commitments"][0]):  # type: ignore[index]
        responsible = str(commitment["responsible"])
        assert "@" not in responsible
        assert responsible.startswith("[redacted]")
        assert len(responsible) <= 120
