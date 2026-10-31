"""Writes: confirmed exactly, executed once, never retried blindly (brief sections 3 and 5)."""

import json
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from cortex.bootstrap import HarnessData, build_runner
from cortex.kit_adapter.audit import Audit
from cortex.kit_adapter.orchestrator import run_scenario_file
from cortex.memory.database import Database
from cortex.memory.ledger import OperationStatus, PendingStatus
from cortex.runner.agent_runner import RunRequest, RunResult
from cortex.runner.finish import FinishDetail
from cortex.runner.scripted_model import ScriptedModel
from cortex.timekeeping import ManualTicker
from cortex.tools.adapters.claims import ClaimsDesk
from cortex.tools.failures import PermanentFailure, UncertainOutcome
from cortex.tools.gateway import ToolAdapter, WriteAdapter
from cortex.tools.policy import Approved, PolicyContext, review
from cortex.tools.writes import Done, Refused, WriteContext, write
from merchant_assistant.agent.domain.messages import ToolCall
from merchant_assistant.control.states import State
from merchant_assistant.shared.clock import FixedClock

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[3]
EXAMPLES, KIT = ROOT / "examples", ROOT / "kit"
CLOCK = FixedClock(datetime(2026, 10, 28, 12, tzinfo=UTC))
CLAIM = {"motivo": "demora", "descripcion": "No recibi comprobante", "operation_id": "op_claim_001"}
ASK = {"tool": "crear_reclamo", "args": CLAIM}

AdapterWrapper = Any  # a callable that wraps the real claims adapter


@dataclass
class Rig:
    """One company's harness over files that outlive any single runner."""

    workdir: Path
    data: HarnessData
    wrap_claims: AdapterWrapper = None

    def __post_init__(self) -> None:
        self.auth = self.data.directory.authenticate("comercio_a", "operador_a")
        self.restart()

    def restart(self) -> None:
        """A new process: new objects over the same files, after the startup recovery."""
        self.database = Database(self.workdir / "cortex.db", CLOCK)
        self.database.initialize()
        self.database.recover()
        self.desk = ClaimsDesk(self.workdir / "claims", CLOCK)
        self.claims_adapter: WriteAdapter = self.desk.for_tenant(self.auth)
        if self.wrap_claims is not None:
            self.claims_adapter = self.wrap_claims(self.claims_adapter)

    def run(self, message: str, script: list[dict[str, Any]], run_id: str) -> RunResult:
        adapters: dict[str, ToolAdapter] = {"crear_reclamo": self.claims_adapter}
        runner = build_runner(
            data=self.data,
            database=self.database,
            model=ScriptedModel(script),
            bind_tools=lambda auth: adapters,
            clock=CLOCK,
            ticker=ManualTicker(),
        )
        return runner.run(RunRequest(run_id, "conv_1", message, self.auth, self.data.config.limits))

    @property
    def claims(self) -> int:
        return Audit(self.database.path, self.desk).claims("comercio_a")[0]

    def types(self, result: RunResult) -> list[str]:
        return [event.type for event in result.events]


@pytest.fixture
def rig(tmp_path: Path, harness_data: HarnessData) -> Rig:
    return Rig(tmp_path, harness_data)


def _variant(tmp_path: Path, **changes: object) -> Path:
    data = json.loads((EXAMPLES / "T09.json").read_text(encoding="utf-8"))
    data.update(changes)
    path = tmp_path / "T09-variant.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- the kit's T09


def test_t09_the_same_operation_id_twice_files_one_claim() -> None:
    output: dict[str, Any] = run_scenario_file(EXAMPLES / "T09.json", kit_dir=KIT)
    types = [event["type"] for event in output["events"]]

    assert (output["unique_claims_created"], output["claims_created"]) == (1, 1)
    assert types.count("tool_executed") == 1
    assert types.count("tool_deduplicated") == 1
    assert output["finish_reason"] == "COMPLETED"


def test_without_a_confirmation_nothing_is_written_and_the_user_is_asked(tmp_path: Path) -> None:
    output: dict[str, Any] = run_scenario_file(_variant(tmp_path, preconditions={}), kit_dir=KIT)

    assert (output["finish_reason"], output["finish_detail"]) == (
        "NEEDS_HUMAN",
        "awaiting_confirmation",
    )
    assert output["claims_created"] == 0
    # The question answers the user's message: a reply, not a contact.
    assert output["effects"]["ai_replies_delivered"] == 1
    assert output["effects"]["customer_messages_sent"] == 0


# --------------------------------------------------------------------------- confirmation


def test_the_question_shows_the_exact_values_and_a_yes_runs_the_write(rig: Rig) -> None:
    asked = rig.run("Quiero reclamar", [ASK], "run-1")
    pending = rig.database.for_tenant(rig.auth).ledger.pending_action("op_claim_001")

    assert asked.finish_detail is FinishDetail.AWAITING_CONFIRMATION
    assert pending is not None
    assert pending.status is PendingStatus.PENDING
    assert rig.claims == 0

    confirmed = rig.run("sí", [{"final": "Registrado"}], "run-2")

    assert confirmed.finish_detail is FinishDetail.ANSWERED
    assert "confirmation_received" in rig.types(confirmed)
    assert rig.claims == 1

    again = rig.run("sí", [{"final": "Ya estaba"}], "run-3")  # nothing pending any more
    assert "confirmation_received" not in rig.types(again)
    assert rig.claims == 1


@pytest.mark.parametrize("reply", ["no", "sí, pero cambiá la descripción", "¿sí?"])
def test_anything_but_a_clear_yes_cancels_the_pending_write(rig: Rig, reply: str) -> None:
    rig.run("Quiero reclamar", [ASK], "run-1")

    rig.run(reply, [{"final": "Entendido"}], "run-2")

    pending = rig.database.for_tenant(rig.auth).ledger.pending_action("op_claim_001")
    assert pending is not None
    assert pending.status is PendingStatus.CANCELLED
    assert rig.claims == 0


def test_changed_values_after_a_confirmation_need_a_new_one(tmp_path: Path) -> None:
    changed = {**CLAIM, "descripcion": "Tampoco recibi la factura"}
    variant = _variant(
        tmp_path, model_script=[{"tool": "crear_reclamo", "args": changed}, {"final": "Listo"}]
    )

    output = run_scenario_file(variant, kit_dir=KIT)

    assert output["finish_detail"] == "awaiting_confirmation"
    assert output["claims_created"] == 0


def test_an_operation_id_reused_with_other_values_is_refused(rig: Rig) -> None:
    rig.run("Quiero reclamar", [ASK], "run-1")
    rig.run("sí", [{"final": "Registrado"}], "run-2")
    reused = {**CLAIM, "motivo": "otro motivo"}

    result = rig.run(
        "Otro reclamo", [{"tool": "crear_reclamo", "args": reused}, {"final": "No"}], "run-3"
    )

    rejection = next(e for e in result.events if e.type == "tool_rejected")
    assert rejection.attributes["reason"] == "operation_id_conflict"
    assert rig.claims == 1


# --------------------------------------------------------------------------- idempotency


def test_the_same_command_after_a_restart_still_files_one_claim(rig: Rig) -> None:
    rig.run("Quiero reclamar", [ASK], "run-1")
    rig.run("sí", [{"final": "Registrado"}], "run-2")

    rig.restart()
    result = rig.run("Repetí", [ASK, {"final": "Ya estaba"}], "run-3")

    assert "tool_deduplicated" in rig.types(result)
    assert rig.claims == 1


def test_a_repeated_run_id_replays_its_result_without_acting_again(rig: Rig) -> None:
    rig.run("Quiero reclamar", [ASK], "run-1")
    first = rig.run("sí", [{"final": "Registrado"}], "run-2")
    store = rig.database.for_tenant(rig.auth)
    messages_before = len(store.recent_messages("conv_1", 50))

    replay = rig.run("sí", [{"final": "Registrado"}], "run-2")

    assert replay == first
    assert rig.claims == 1
    assert len(store.recent_messages("conv_1", 50)) == messages_before


def test_a_run_id_reused_for_another_message_is_blocked(rig: Rig) -> None:
    rig.run("Hola", [{"final": "Hola"}], "run-1")

    result = rig.run("Otra cosa", [{"final": "?"}], "run-1")

    assert result.finish_detail is FinishDetail.RUN_ID_CONFLICT


def test_concurrent_executions_of_one_command_have_one_effect(
    tmp_path: Path, harness_data: HarnessData
) -> None:
    rig = Rig(tmp_path, harness_data)
    rig.run("Quiero reclamar", [ASK], "run-1")
    ledger = rig.database.for_tenant(rig.auth).ledger
    pending = ledger.pending_action("op_claim_001")
    assert pending is not None
    ledger.move_pending(
        "op_claim_001", from_status=PendingStatus.PENDING, to_status=PendingStatus.CONFIRMED
    )
    approved = review(
        ToolCall("c", "crear_reclamo", json.dumps(CLAIM)),
        harness_data.catalog,
        PolicyContext(rig.auth, State.AUTO, CLOCK.now().date()),
    )
    assert isinstance(approved, Approved)
    barrier = threading.Barrier(8)
    outcomes: list[object] = []

    def attempt(index: int) -> None:
        context = WriteContext(ledger, "conv_1", 0, f"run-thread-{index}")
        barrier.wait()
        outcomes.append(write(approved, rig.claims_adapter, context))

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert rig.claims == 1
    assert all(isinstance(outcome, Done | Refused) for outcome in outcomes)
    assert sum(isinstance(o, Done) and o.how == "executed" for o in outcomes) == 1


# --------------------------------------------------------------------------- uncertain outcomes


class _Flaky:
    """Wraps the real claims adapter to simulate what the claims system can do."""

    def __init__(
        self, real: WriteAdapter, *, commit_first: bool, failures: int, lookup_fails: bool = False
    ) -> None:
        self.real, self.commit_first, self.failures, self.lookup_fails = (
            real,
            commit_first,
            failures,
            lookup_fails,
        )
        self.tenant_id = real.tenant_id
        self.creates = 0

    def __call__(self, arguments: BaseModel, *, conversation_id: str) -> Mapping[str, object]:
        self.creates += 1
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


def _recording(made: list[Any], factory: Callable[[WriteAdapter], Any]) -> AdapterWrapper:
    """Wraps each new claims adapter with `factory` and keeps the wrapper for the test."""

    def wrap(real: WriteAdapter) -> Any:
        adapter = factory(real)
        made.append(adapter)
        return adapter

    return wrap


def _confirmed_rig(tmp_path: Path, data: HarnessData, flaky: Any) -> Rig:
    rig = Rig(tmp_path, data, wrap_claims=flaky)
    rig.run("Quiero reclamar", [ASK], "run-1")
    return rig


def test_a_write_committed_before_a_timeout_is_reconciled_not_repeated(
    tmp_path: Path, harness_data: HarnessData
) -> None:
    flaky: list[_Flaky] = []
    rig = _confirmed_rig(
        tmp_path,
        harness_data,
        _recording(flaky, lambda real: _Flaky(real, commit_first=True, failures=1)),
    )

    result = rig.run("sí", [{"final": "Registrado"}], "run-2")

    assert "write_reconciled" in rig.types(result)
    assert flaky[-1].creates == 1  # looked up, never sent again
    assert rig.claims == 1


def test_a_write_that_never_landed_is_attempted_once_more(
    tmp_path: Path, harness_data: HarnessData
) -> None:
    flaky: list[_Flaky] = []
    rig = _confirmed_rig(
        tmp_path,
        harness_data,
        _recording(flaky, lambda real: _Flaky(real, commit_first=False, failures=1)),
    )

    result = rig.run("sí", [{"final": "Registrado"}], "run-2")

    assert "write_reconciled" in rig.types(result)
    assert flaky[-1].creates == 2
    assert rig.claims == 1


def test_an_outcome_that_cannot_be_checked_fails_the_run_and_is_reconciled_later(
    tmp_path: Path, harness_data: HarnessData
) -> None:
    flaky: list[_Flaky] = []
    rig = _confirmed_rig(
        tmp_path,
        harness_data,
        _recording(
            flaky, lambda real: _Flaky(real, commit_first=True, failures=1, lookup_fails=True)
        ),
    )

    failed = rig.run("sí", [{"final": "Registrado"}], "run-2")
    ledger = rig.database.for_tenant(rig.auth).ledger
    operation = ledger.operation("op_claim_001")

    assert failed.finish_detail is FinishDetail.WRITE_OUTCOME_UNKNOWN
    assert operation is not None
    assert operation.status is OperationStatus.UNCERTAIN

    flaky[-1].lookup_fails = False
    later = rig.run("¿Se registró?", [ASK, {"final": "Sí"}], "run-3")

    assert "write_reconciled" in rig.types(later)
    assert flaky[-1].creates == 1
    assert rig.claims == 1


def test_a_certain_refusal_is_not_retried(tmp_path: Path, harness_data: HarnessData) -> None:
    class Refusing:
        def __init__(self, real: WriteAdapter) -> None:
            self.tenant_id, self.real, self.creates = real.tenant_id, real, 0

        def __call__(self, arguments: BaseModel, *, conversation_id: str) -> Mapping[str, object]:
            self.creates += 1
            raise PermanentFailure("rejected by the claims system")

        def lookup(self, operation_id: str) -> Mapping[str, object] | None:
            return self.real.lookup(operation_id)

    refusing: list[Refusing] = []
    rig = _confirmed_rig(tmp_path, harness_data, _recording(refusing, lambda real: Refusing(real)))

    result = rig.run("sí", [{"final": "No se pudo"}], "run-2")
    operation = rig.database.for_tenant(rig.auth).ledger.operation("op_claim_001")

    assert "tool_error" in rig.types(result)
    assert refusing[-1].creates == 1
    assert operation is not None
    assert operation.status is OperationStatus.FAILED
    assert rig.claims == 0


def test_the_startup_recovery_marks_in_flight_work(rig: Rig) -> None:
    rig.run("Quiero reclamar", [ASK], "run-1")
    store = rig.database.for_tenant(rig.auth)
    assert store.ledger.confirm_pending("op_claim_001", "conv_1", 0)
    pending = store.ledger.pending_action("op_claim_001")
    assert pending is not None
    assert pending.fingerprint is not None
    assert store.runs.admit("run-9", "conv_1", "d").admission == "ADMITTED"
    started = store.ledger.start_operation(
        "op_claim_001",
        "crear_reclamo",
        pending.fingerprint,
        "run-9",
        conversation_id="conv_1",
        control_version=0,
    )
    assert started == "STARTED"

    rig.restart()  # the process died with run-9 running and the write in flight

    store = rig.database.for_tenant(rig.auth)
    operation = store.ledger.operation("op_claim_001")
    assert operation is not None
    assert operation.status is OperationStatus.UNCERTAIN
    # The same command is not run again: it may already have answered before the crash.
    assert store.runs.admit("run-9", "conv_1", "d").admission == "INTERRUPTED"
    assert store.runs.admit("run-10", "conv_1", "d").admission == "ADMITTED"  # the lease was freed
