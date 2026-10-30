"""What the model receives: bounded history, structured state, results marked as untrusted."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cortex.bootstrap import HarnessData, build_runner, zoco_tool_binder
from cortex.memory.chat_records import Commitment
from cortex.memory.database import Database
from cortex.memory.ledger import PendingAction, PendingStatus
from cortex.runner.agent_runner import RunRequest
from cortex.runner.finish import FinishReason
from cortex.runner.scripted_model import ScriptedModel
from cortex.timekeeping import ManualTicker
from cortex.tools.adapters.claims import ClaimsDesk
from merchant_assistant.shared.clock import FixedClock

pytestmark = pytest.mark.integration


def test_the_model_sees_its_own_company_data_as_untrusted_tool_results(
    tmp_path: Path, harness_data: HarnessData
) -> None:
    clock = FixedClock(datetime(2026, 10, 28, 12, tzinfo=UTC))
    database = Database(tmp_path / "cortex.db", clock)
    database.initialize()
    model = ScriptedModel(
        [{"tool": "consultar_ventas", "args": {"fecha": "2026-10-15"}}, {"final": "8 ventas"}]
    )
    runner = build_runner(
        data=harness_data,
        database=database,
        model=model,
        bind_tools=zoco_tool_binder(
            harness_data, ClaimsDesk(tmp_path / "claims", clock), clock=clock
        ),
        clock=clock,
        ticker=ManualTicker(),
    )
    auth = harness_data.directory.authenticate("comercio_b", "operador_b")

    result = runner.run(
        RunRequest("run-1", "conv_1", "¿Cuánto vendí el 15?", auth, harness_data.config.limits)
    )

    assert result.finish_reason is FinishReason.COMPLETED
    second_prompt = model.prompts[1]
    envelope = json.loads(second_prompt.messages[-1].content)
    assert second_prompt.messages[0].content == "¿Cuánto vendí el 15?"
    assert envelope["untrusted_data"] is True
    assert envelope["data"]["cantidad"] == 8  # comercio_b's figure; comercio_a had 15
    assert second_prompt.state == {
        "conversation_mode": "AUTO",
        "control_version": 0,
        "pending_actions": [],
        "commitments": [],
    }


def test_the_history_sent_to_the_model_is_bounded_but_critical_state_is_not_summarized(
    tmp_path: Path, harness_data: HarnessData
) -> None:
    clock = FixedClock(datetime(2026, 10, 28, 12, tzinfo=UTC))
    database = Database(tmp_path / "cortex.db", clock)
    database.initialize()
    auth = harness_data.directory.authenticate("comercio_a", "operador_a")
    store = database.for_tenant(auth)
    store.open_conversation("conv_1")
    store.ledger.record_pending(
        PendingAction(
            operation_id="op_old",
            conversation_id="conv_1",
            tool="crear_reclamo",
            arguments=json.dumps(
                {
                    "descripcion": "No recibi comprobante",
                    "motivo": "demora",
                    "operation_id": "op_old",
                }
            ),
            fingerprint="f",
            control_version=0,
            # Kept for the operator: a stray message must not cancel it.
            status=PendingStatus.SUSPENDED,
        )
    )
    store.chat.record_followup(
        "conv_1",
        "run-0",
        "digest-0",
        "TAREA_INTERNA",
        ("ZOCO_OWES_ACTION",),
        Commitment("internal_task", "Benjamin", None),
    )
    # Both were recorded long ago: 25 turns of small talk came after them.
    for turn in range(1, 26):
        store.append_message("conv_1", f"old-{turn}", "user", f"mensaje {turn}")
        store.append_message("conv_1", f"old-{turn}", "assistant", f"respuesta {turn}")
    model = ScriptedModel([{"final": "OK"}])
    runner = build_runner(
        data=harness_data,
        database=database,
        model=model,
        bind_tools=zoco_tool_binder(
            harness_data, ClaimsDesk(tmp_path / "claims", clock), clock=clock
        ),
        clock=clock,
        ticker=ManualTicker(),
    )

    runner.run(RunRequest("run-1", "conv_1", "¿Y mi reclamo?", auth, harness_data.config.limits))

    prompt = model.prompts[0]
    window = harness_data.config.limits.context_window_messages
    assert 0 < len(prompt.messages) <= window
    assert prompt.messages[0].role == "user"  # a window never starts in the middle of a turn
    assert prompt.messages[-1].content == "¿Y mi reclamo?"
    assert "mensaje 1" not in [m.content for m in prompt.messages]  # the old turns fell out
    pending = prompt.state["pending_actions"]
    assert isinstance(pending, list)
    assert pending[0]["arguments"]["descripcion"] == "No recibi comprobante"  # exact values
    assert prompt.state["commitments"] == [
        {"kind": "internal_task", "responsible": "Benjamin", "due_date": None}
    ]
