"""Zoco Chat (brief section 6): takeover, restart, continuity card and follow-ups, end to end."""

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cortex.chat.service import ChatEvents
from cortex.kit_adapter.orchestrator import run_scenario_file
from cortex.memory.tenant_store import TenantStore
from cortex.tools.adapters.claims import ClaimsAdapter

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[3]
EXAMPLES, KIT = ROOT / "examples", ROOT / "kit"
CHAT_EVENTS = json.loads((KIT / "fixtures" / "chat_events.json").read_text(encoding="utf-8"))
CLAIM = {"motivo": "demora", "descripcion": "No recibi comprobante", "operation_id": "op_claim_001"}


def _run(tmp_path: Path, base: str, **changes: Any) -> dict[str, Any]:
    data = json.loads((EXAMPLES / f"{base}.json").read_text(encoding="utf-8"))
    data.update(changes)
    path = tmp_path / f"{base}-variant.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    output: dict[str, Any] = run_scenario_file(path, kit_dir=KIT)
    return output


def _types(output: dict[str, Any]) -> list[str]:
    return [event["type"] for event in output["events"]]


# --------------------------------------------------------------------------- the kit's scenarios


def test_t10_a_restart_keeps_the_human_in_control_and_the_pending_action(tmp_path: Path) -> None:
    output = _run(tmp_path, "T10")
    card = output["state"]["continuity_card"]

    assert (output["state_after_restart"], output["pending_action_retained"]) == ("HUMAN", True)
    assert (output["finish_reason"], output["step_count"]) == ("NEEDS_HUMAN", 0)  # no model call
    assert (output["customer_messages_sent"], output["ai_replies_delivered"]) == (0, 0)
    assert card["pending_actions"][0]["operation_id"] == "op_pending_001"
    assert card["pending_actions"][0]["status"] == "SUSPENDED"


def test_t11_a_reply_generated_before_the_takeover_is_never_delivered(tmp_path: Path) -> None:
    output = _run(tmp_path, "T11")
    card = output["state"]["continuity_card"]

    assert output["delivered_ai_messages_after_takeover"] == 0
    assert output["tool_effects_after_takeover"] == 0
    assert (output["customer_messages_sent"], output["ai_replies_delivered"]) == (0, 0)
    assert (output["finish_reason"], output["finish_detail"]) == (
        "NEEDS_HUMAN",
        "operator_took_over",
    )
    assert "reply_suppressed" in _types(output)
    assert (card["mode"], card["control_version"], card["responsible"]) == (
        "HUMAN",
        2,
        "operador_a",
    )
    assert card["last_reply_suppressed"] == "OK"  # the operator sees what was held back


@pytest.mark.parametrize(("scenario", "decision"), [("T12", "TAREA_INTERNA"), ("T13", "CANCELAR")])
def test_follow_ups_are_decided_by_code_and_send_nothing(
    tmp_path: Path, scenario: str, decision: str
) -> None:
    output = _run(tmp_path, scenario)

    assert output["decision"] == decision
    assert (output["customer_messages_sent"], output["ai_replies_delivered"]) == (0, 0)
    assert output["step_count"] == 0  # the scripted "OK" is never even generated


def test_t12_leaves_an_internal_task_for_the_responsible(tmp_path: Path) -> None:
    card = _run(tmp_path, "T12")["state"]["continuity_card"]

    assert card["commitments"] == [
        {"kind": "internal_task", "responsible": "Benjamin", "due_date": None}
    ]


# --------------------------------------------------------------------------- takeover variants


def test_a_takeover_after_a_tool_request_stops_the_write(tmp_path: Path) -> None:
    output = _run(
        tmp_path,
        "T11",
        preconditions={
            "current_state": "AUTO",
            "current_version": 1,
            "confirmed_actions": [{"tool": "crear_reclamo", "args": CLAIM}],
        },
        model_script=[{"tool": "crear_reclamo", "args": CLAIM}, {"final": "Listo"}],
        incoming_event={
            "type": "operator_took_over",
            "version": 2,
            "when": "after_tool_request_before_execution",
        },
    )
    pending = output["state"]["continuity_card"]["pending_actions"]

    assert output["claims_created"] == 0
    assert output["tool_effects_after_takeover"] == 0
    assert output["executed_tools"] == []
    assert output["finish_detail"] == "operator_took_over"
    assert [p["status"] for p in pending] == ["SUSPENDED"]  # kept for the operator, not runnable


def test_a_takeover_after_a_tool_request_is_a_control_change_not_a_rejected_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    real_call, real_lookup = ClaimsAdapter.__call__, ClaimsAdapter.lookup

    def counted_call(self: ClaimsAdapter, *args: Any, **kwargs: Any) -> Any:
        calls.append("create")
        return real_call(self, *args, **kwargs)

    def counted_lookup(self: ClaimsAdapter, operation_id: str) -> Any:
        calls.append("lookup")
        return real_lookup(self, operation_id)

    monkeypatch.setattr(ClaimsAdapter, "__call__", counted_call)
    monkeypatch.setattr(ClaimsAdapter, "lookup", counted_lookup)
    output = _run(
        tmp_path,
        "T11",
        preconditions={
            "current_state": "AUTO",
            "current_version": 1,
            "confirmed_actions": [{"tool": "crear_reclamo", "args": CLAIM}],
        },
        model_script=[{"tool": "crear_reclamo", "args": CLAIM}, {"final": "Listo"}],
        incoming_event={
            "type": "operator_took_over",
            "version": 2,
            "when": "after_tool_request_before_execution",
        },
    )

    assert (output["finish_reason"], output["finish_detail"]) == (
        "NEEDS_HUMAN",
        "operator_took_over",
    )
    assert "checkpoint_failed" in _types(output)
    assert "tool_rejected" not in _types(output)
    assert output["rejected_tools"] == []
    assert output["state"]["continuity_card"]["last_tool"] is None  # nothing "rejected" shown
    assert calls == []


def test_a_takeover_before_the_run_means_the_model_is_never_called(tmp_path: Path) -> None:
    output = _run(tmp_path, "T11", incoming_event={"type": "operator_took_over", "version": 2})

    assert (output["finish_detail"], output["step_count"]) == ("human_in_control", 0)
    assert (output["customer_messages_sent"], output["ai_replies_delivered"]) == (0, 0)


def test_a_stale_takeover_changes_nothing(tmp_path: Path) -> None:
    output = _run(
        tmp_path,
        "T11",
        preconditions={"current_state": "AUTO", "current_version": 2},
        incoming_event={
            "type": "operator_took_over",
            "version": 1,
            "when": "after_response_generation_before_delivery",
        },
    )

    assert output["chat_events"][0]["type"] == "chat_event_ignored"
    assert (output["finish_reason"], output["ai_replies_delivered"]) == ("COMPLETED", 1)
    assert output["customer_messages_sent"] == 0


def test_a_takeover_at_the_stored_version_while_the_agent_answers_is_applied(
    tmp_path: Path,
) -> None:
    """Two reports of the same version disagree on who controls the chat: the person wins."""
    output = _run(
        tmp_path,
        "T11",
        incoming_event={
            "type": "operator_took_over",
            "version": 1,
            "when": "after_response_generation_before_delivery",
        },
    )

    assert output["chat_events"][0] == {
        "type": "chat_event_applied",
        "version": 1,
        "responsible": "operador_a",
        "conflict": True,
    }
    assert (output["finish_reason"], output["finish_detail"]) == (
        "NEEDS_HUMAN",
        "operator_took_over",
    )
    assert output["delivered_ai_messages_after_takeover"] == 0
    assert (output["state"]["conversation_mode"], output["state"]["control_version"]) == (
        "HUMAN",
        2,
    )


def test_an_operator_of_another_company_cannot_take_the_conversation(tmp_path: Path) -> None:
    output = _run(
        tmp_path,
        "T11",
        incoming_event={"type": "operator_took_over", "version": 2, "actor": "operador_b"},
    )

    assert output["chat_events"] == [{"type": "chat_event_rejected", "reason": "auth_invalid"}]
    assert output["state"]["conversation_mode"] == "AUTO"
    assert (output["ai_replies_delivered"], output["customer_messages_sent"]) == (1, 0)


def test_an_event_for_another_company_is_refused(tmp_path: Path) -> None:
    takeover = _run(
        tmp_path,
        "T11",
        incoming_event={"type": "operator_took_over", "version": 2, "tenant_id": "comercio_b"},
    )
    follow_up = _run(
        tmp_path,
        "T12",
        incoming_event={"type": "followup_due", "pending_on": "ZOCO", "tenant_id": "comercio_b"},
    )

    assert takeover["chat_events"] == [{"type": "chat_event_rejected", "reason": "scope_violation"}]
    assert (follow_up["finish_reason"], follow_up["decision"]) == ("BLOCKED", None)


def test_the_takeover_measurement_notices_a_delivery_that_ignores_the_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def careless(
        self: TenantStore,
        conversation_id: str,
        run_id: str,
        text: str,
        expected_version: int,
        *,
        reply_to: int | None,
    ) -> bool:
        """Sends whatever happens: no mode check, no version check."""
        with self._db.transaction() as tx:
            tx.execute(
                "INSERT INTO outbox (tenant_id, conversation_id, run_id, content, status, kind, "
                "reply_to, control_version, created_at) "
                "VALUES (?, ?, ?, ?, 'SENT', 'reply', ?, ?, 'now')",
                (self.tenant_id, conversation_id, run_id, text, reply_to, expected_version),
            )
            tx.execute(
                "INSERT INTO journal (tenant_id, conversation_id, kind, reference, created_at) "
                "VALUES (?, ?, 'delivery', ?, 'now')",
                (self.tenant_id, conversation_id, run_id),
            )
        return True

    monkeypatch.setattr(TenantStore, "deliver_if_current", careless)
    output = _run(tmp_path, "T11")

    assert output["delivered_ai_messages_after_takeover"] == 1
    assert output["ai_replies_delivered"] == 1


def _contacting_follow_up(monkeypatch: pytest.MonkeyPatch, *, as_a_reply: bool) -> None:
    """A bug: the follow-up handler also sends the customer the scripted "OK"."""
    decide = ChatEvents.follow_up

    def contacting(
        self: ChatEvents, auth: Any, conversation_id: str, run_id: str, *args: Any
    ) -> Any:
        decision = decide(self, auth, conversation_id, run_id, *args)
        store = self.database.for_tenant(auth)
        version = store.open_conversation(conversation_id).control_version
        # As a reply it even pretends to answer a message of its own run: still not a reply.
        reply_to = (
            store.append_message(conversation_id, run_id, "user", "Hola") if as_a_reply else None
        )
        store.deliver_if_current(conversation_id, run_id, "OK", version, reply_to=reply_to)
        return decision

    monkeypatch.setattr(ChatEvents, "follow_up", contacting)


@pytest.mark.parametrize("as_a_reply", [False, True], ids=["proactive", "labelled_reply"])
@pytest.mark.parametrize("scenario", ["T12", "T13"])
def test_a_follow_up_that_contacts_the_customer_is_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str, as_a_reply: bool
) -> None:
    _contacting_follow_up(monkeypatch, as_a_reply=as_a_reply)

    output = _run(tmp_path, scenario)

    assert output["customer_messages_sent"] == 1
    assert output["effects"]["customer_messages_sent"] == 1
    assert output["ai_replies_delivered"] == 0


# --------------------------------------------------------------------------- follow-up fixtures


@pytest.mark.parametrize("event", CHAT_EVENTS["events"][1:], ids=lambda e: e["id"])
def test_the_kits_chat_events_get_their_decision(tmp_path: Path, event: dict[str, Any]) -> None:
    raw = {key: value for key, value in event.items() if key != "id"}
    output = _run(
        tmp_path,
        "T12",
        tenant_id=event["tenant_id"],
        principal_id="operador_a" if event["tenant_id"] == "comercio_a" else "operador_b",
        conversation_id=event["conversation_id"],
        incoming_event=raw,
    )

    assert output["decision"] == event["expected_decision"]


@pytest.mark.parametrize(
    ("index", "decision"), [(1, "TAREA_INTERNA"), (2, "REPROGRAMAR")], ids=["E02", "E03"]
)
def test_a_kit_event_pasted_verbatim_acts_on_the_conversation_it_names(
    tmp_path: Path, index: int, decision: str
) -> None:
    event = CHAT_EVENTS["events"][index]
    output = _run(tmp_path, "T12", incoming_event=event)

    assert (output["finish_reason"], output["decision"]) == ("COMPLETED", decision)
    assert output["conversation_id"] == event["conversation_id"]
    assert output["run_id"] == f"run-{event['conversation_id']}"
    assert output["state"]["continuity_card"]["commitments"]  # owed on that conversation


def test_a_takeover_pasted_verbatim_acts_on_the_conversation_it_names(tmp_path: Path) -> None:
    e01 = CHAT_EVENTS["events"][0]
    output = _run(tmp_path, "T11", incoming_event=e01)

    assert output["conversation_id"] == "conv_a_handoff"
    assert (output["finish_detail"], output["step_count"]) == ("human_in_control", 0)
    assert output["state"]["conversation_mode"] == "HUMAN"


def test_an_event_naming_a_conversation_of_another_company_is_still_refused(
    tmp_path: Path,
) -> None:
    e04 = CHAT_EVENTS["events"][3]  # comercio_b's event, given to a scenario of comercio_a
    output = _run(tmp_path, "T12", incoming_event=e04)

    assert (output["finish_reason"], output["finish_detail"]) == ("BLOCKED", "scope_violation")
    assert output["conversation_id"] == "conv_t12"
    assert output["decision"] is None


def test_the_rescheduling_depends_on_the_fixed_clock_not_on_the_day_it_runs(tmp_path: Path) -> None:
    e03 = {key: value for key, value in CHAT_EVENTS["events"][2].items() if key != "id"}
    path = tmp_path / "e03.json"
    data = json.loads((EXAMPLES / "T12.json").read_text(encoding="utf-8"))
    data.update(conversation_id=e03["conversation_id"], incoming_event=e03)
    path.write_text(json.dumps(data), encoding="utf-8")
    later = run_scenario_file(path, kit_dir=KIT, now=datetime(2026, 11, 10, 12, tzinfo=UTC))

    assert run_scenario_file(path, kit_dir=KIT)["decision"] == "REPROGRAMAR"
    assert later["decision"] == "CONTACTAR"


# --------------------------------------------------------------------------- a real restart


_STEP = """
import sys
from pathlib import Path
from datetime import UTC, datetime
from merchant_assistant.shared.clock import FixedClock
from cortex.bootstrap import build_runner, load_harness_data, zoco_tool_binder
from cortex.memory.database import Database
from cortex.runner.agent_runner import RunRequest
from cortex.runner.scripted_model import ScriptedModel
from cortex.timekeeping import ManualTicker
from cortex.tools.adapters.claims import ClaimsDesk
import json

kit, workdir, run_id, message, script = sys.argv[1:6]
clock = FixedClock(datetime(2026, 10, 28, 12, tzinfo=UTC))
data = load_harness_data(Path(kit))
database = Database(Path(workdir) / "cortex.db", clock)
database.initialize()
database.recover()
runner = build_runner(
    data=data, database=database, model=ScriptedModel(json.loads(script)),
    bind_tools=zoco_tool_binder(data, ClaimsDesk(Path(workdir) / "claims", clock), clock=clock),
    clock=clock, ticker=ManualTicker(),
)
auth = data.directory.authenticate("comercio_a", "operador_a")
result = runner.run(RunRequest(run_id, "conv_1", message, auth, data.config.limits))
print(json.dumps({"detail": result.finish_detail.value, "types": [e.type for e in result.events]}))
"""


def _process(
    tmp_path: Path, run_id: str, message: str, script: list[dict[str, Any]]
) -> dict[str, Any]:
    done = subprocess.run(  # noqa: S603 - the test's own interpreter and script
        [sys.executable, "-c", _STEP, str(KIT), str(tmp_path), run_id, message, json.dumps(script)],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    answer: dict[str, Any] = json.loads(done.stdout)
    return answer


def test_three_separate_processes_file_one_claim(tmp_path: Path) -> None:
    from cortex.kit_adapter.audit import Audit

    asked = _process(
        tmp_path, "run-1", "Quiero reclamar", [{"tool": "crear_reclamo", "args": CLAIM}]
    )
    confirmed = _process(tmp_path, "run-2", "sí", [{"final": "Registrado"}])
    repeated = _process(
        tmp_path,
        "run-3",
        "Repetí",
        [{"tool": "crear_reclamo", "args": CLAIM}, {"final": "Ya estaba"}],
    )

    assert asked["detail"] == "awaiting_confirmation"
    assert "confirmation_received" in confirmed["types"]
    assert "tool_deduplicated" in repeated["types"]
    assert Audit(tmp_path / "cortex.db", tmp_path / "claims").claims("comercio_a") == (1, 1)


@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 10, 28, 12, tzinfo=UTC),  # a working morning in Buenos Aires
        datetime(2026, 10, 28, 3, tzinfo=UTC),  # the middle of the night
        datetime(2026, 10, 31, 15, tzinfo=UTC),  # a Saturday
    ],
)
def test_the_takeover_scenarios_do_not_depend_on_the_time_of_day(
    tmp_path: Path, now: datetime
) -> None:
    """The first delivery's state machine only allowed a takeover in attention hours and sent the
    conversation back to the agent when they ended; the harness applies a takeover whenever it
    is reported."""
    for scenario in ("T10", "T11"):
        path = tmp_path / f"{scenario}.json"
        path.write_text((EXAMPLES / f"{scenario}.json").read_text(encoding="utf-8"))
        output = run_scenario_file(path, kit_dir=KIT, now=now)

        assert output["finish_reason"] == "NEEDS_HUMAN", scenario
        assert (output["customer_messages_sent"], output["ai_replies_delivered"]) == (0, 0), (
            scenario
        )
