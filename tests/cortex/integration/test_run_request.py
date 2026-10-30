"""What a caller can ask of one run (brief section 2): the tools it may use and the control
version it expects."""

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cortex.bootstrap import HarnessData, build_runner, zoco_tool_binder
from cortex.config import MAX_ANSWER_CHARS, MAX_MESSAGE_CHARS
from cortex.kit_adapter.observables import CallCounter
from cortex.memory.database import Database
from cortex.memory.tenant_store import Deliveries
from cortex.runner.agent_runner import RunRequest, RunResult
from cortex.runner.finish import FinishDetail, FinishReason
from cortex.runner.scripted_model import ScriptedModel
from cortex.timekeeping import ManualTicker
from cortex.tools.adapters.claims import ClaimsDesk
from merchant_assistant.control.states import State
from merchant_assistant.shared.clock import FixedClock

pytestmark = pytest.mark.integration

SALES = {"tool": "consultar_ventas", "args": {"fecha": "2026-10-15"}}


@dataclass
class _Rig:
    database: Database
    counter: CallCounter
    model: ScriptedModel
    harness_data: HarnessData

    def run(self, message: str = "Hola", **request: Any) -> RunResult:
        clock = FixedClock(datetime(2026, 10, 28, 12, tzinfo=UTC))
        runner = build_runner(
            data=self.harness_data,
            database=self.database,
            model=self.model,
            bind_tools=self.counter.wrap(
                zoco_tool_binder(
                    self.harness_data, ClaimsDesk(self.database.path.parent, clock), clock=clock
                )
            ),
            clock=clock,
            ticker=ManualTicker(),
        )
        auth = self.harness_data.directory.authenticate("comercio_a", "operador_a")
        return runner.run(
            RunRequest("run-1", "conv_1", message, auth, self.harness_data.config.limits, **request)
        )


def _rig(tmp_path: Path, harness_data: HarnessData, script: list[dict[str, Any]]) -> _Rig:
    clock = FixedClock(datetime(2026, 10, 28, 12, tzinfo=UTC))
    database = Database(tmp_path / "cortex.db", clock)
    database.initialize()
    return _Rig(database, CallCounter(), ScriptedModel(script), harness_data)


def _types(result: RunResult) -> list[str]:
    return [event.type for event in result.events]


# --------------------------------------------------------------------------- tools


def test_without_a_tool_list_the_run_offers_the_whole_catalog(
    tmp_path: Path, harness_data: HarnessData
) -> None:
    rig = _rig(tmp_path, harness_data, [SALES, {"final": "15"}])

    result = rig.run()

    assert set(rig.model.prompts[0].tools) == set(harness_data.catalog.names)
    assert result.finish_reason is FinishReason.COMPLETED
    assert rig.counter.calls == {"consultar_ventas": 1}


def test_a_tool_outside_the_runs_list_is_rejected_and_never_called(
    tmp_path: Path, harness_data: HarnessData
) -> None:
    rig = _rig(tmp_path, harness_data, [SALES, {"final": "No"}])

    result = rig.run(tools=frozenset({"buscar_documentacion"}))

    rejected = next(event for event in result.events if event.type == "tool_rejected")
    assert rig.model.prompts[0].tools == ("buscar_documentacion",)
    assert (rejected.attributes["tool"], rejected.attributes["reason"]) == (
        "consultar_ventas",
        "tool_not_allowed",
    )
    assert rig.counter.calls == {}  # the adapter was never reached
    assert "tool_executed" not in _types(result)


def test_an_empty_tool_list_offers_nothing(tmp_path: Path, harness_data: HarnessData) -> None:
    rig = _rig(tmp_path, harness_data, [SALES, {"final": "No"}])

    rig.run(tools=frozenset())

    assert rig.model.prompts[0].tools == ()
    assert rig.counter.calls == {}


# --------------------------------------------------------------------------- control version


def test_a_run_that_expects_another_control_version_is_blocked_before_the_model(
    tmp_path: Path, harness_data: HarnessData
) -> None:
    rig = _rig(tmp_path, harness_data, [{"final": "OK"}])
    auth = harness_data.directory.authenticate("comercio_a", "operador_a")
    store = rig.database.for_tenant(auth)
    store.open_conversation("conv_1")
    store.chat.set_control("conv_1", State.AUTO, 3)

    result = rig.run(expected_control_version=2)

    assert (result.finish_reason, result.finish_detail) == (
        FinishReason.BLOCKED,
        FinishDetail.STALE_VERSION,
    )
    assert rig.model.prompts == []  # nobody asked the model
    assert store.deliveries("run-1") == Deliveries(0, 0)
    assert _types(result)[-2:] == ["tool_rejected", "run_finished"]
    assert result.events[-2].attributes["reason"] == "stale_version"


def test_a_run_that_expects_the_current_control_version_goes_on(
    tmp_path: Path, harness_data: HarnessData
) -> None:
    rig = _rig(tmp_path, harness_data, [{"final": "OK"}])

    result = rig.run(expected_control_version=0)

    assert result.finish_detail is FinishDetail.ANSWERED


# --------------------------------------------------------------------------- size of the text


def test_a_message_over_the_limit_is_blocked_before_it_is_stored_or_the_model_is_asked(
    tmp_path: Path, harness_data: HarnessData
) -> None:
    rig = _rig(tmp_path, harness_data, [{"final": "OK"}])
    auth = harness_data.directory.authenticate("comercio_a", "operador_a")

    result = rig.run(message="x" * (MAX_MESSAGE_CHARS + 1))

    store = rig.database.for_tenant(auth)
    assert (result.finish_reason, result.finish_detail) == (
        FinishReason.BLOCKED,
        FinishDetail.MESSAGE_TOO_LONG,
    )
    assert rig.model.prompts == []  # nobody asked the model
    assert store.recent_messages("conv_1", 10) == ()  # nothing was stored
    assert store.deliveries("run-1") == Deliveries(0, 0)
    assert _types(result)[-2:] == ["tool_rejected", "run_finished"]
    assert result.events[-2].attributes["reason"] == "message_too_long"


def test_a_message_that_utf8_cannot_encode_is_refused_as_an_invalid_request(
    tmp_path: Path, harness_data: HarnessData
) -> None:
    rig = _rig(tmp_path, harness_data, [{"final": "OK"}])

    with pytest.raises(ValueError, match="UTF-8"):
        rig.run("hola \ud800")

    assert rig.model.prompts == []


def test_a_message_at_the_limit_goes_on(tmp_path: Path, harness_data: HarnessData) -> None:
    rig = _rig(tmp_path, harness_data, [{"final": "OK"}])

    result = rig.run(message="x" * MAX_MESSAGE_CHARS)

    assert result.finish_detail is FinishDetail.ANSWERED


def test_a_final_answer_over_the_limit_is_invalid_model_output_and_is_not_delivered(
    tmp_path: Path, harness_data: HarnessData
) -> None:
    rig = _rig(tmp_path, harness_data, [{"final": "y" * (MAX_ANSWER_CHARS + 1)}])
    auth = harness_data.directory.authenticate("comercio_a", "operador_a")

    result = rig.run()

    failed = next(event for event in result.events if event.type == "model_failed")
    assert (result.finish_reason, result.finish_detail) == (
        FinishReason.FAILED,
        FinishDetail.INVALID_MODEL_OUTPUT,
    )
    assert failed.attributes["code"] == "invalid_model_output"
    assert rig.database.for_tenant(auth).deliveries("run-1") == Deliveries(0, 0)


def test_a_final_answer_at_the_limit_is_delivered(
    tmp_path: Path, harness_data: HarnessData
) -> None:
    rig = _rig(tmp_path, harness_data, [{"final": "y" * MAX_ANSWER_CHARS}])

    assert rig.run().finish_detail is FinishDetail.ANSWERED
