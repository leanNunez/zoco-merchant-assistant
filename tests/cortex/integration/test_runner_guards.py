"""Guards of the runner that no other test pins down: each test fails when its guard is removed.

- The per-conversation lease (`AND active_run_id IS NULL` in `RunJournal.admit`).
- Checkpoint A, the control check before every model call.
- The bound of a model call by the time the run has left.
"""

import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from cortex.bootstrap import HarnessData, build_runner, zoco_tool_binder
from cortex.identity import AuthContext
from cortex.memory.database import Database
from cortex.runner.agent_runner import AgentRunner, RunRequest, RunResult
from cortex.runner.finish import FinishDetail, FinishReason
from cortex.runner.model import ModelPort, ModelTurn, Prompt
from cortex.runner.scripted_model import ScriptedModel
from cortex.timekeeping import ManualTicker
from cortex.tools.adapters.claims import ClaimsDesk
from cortex.tools.gateway import ToolAdapter, ToolBinder
from merchant_assistant.agent.domain.tools import CONSULTAR_VENTAS
from merchant_assistant.shared.clock import FixedClock

pytestmark = pytest.mark.integration

CLOCK = FixedClock(datetime(2026, 10, 28, 12, tzinfo=UTC))
READ_SALES = {"tool": CONSULTAR_VENTAS, "args": {"fecha": "2026-10-15"}}
# Long enough for a slow machine, short enough that a broken test never hangs for long.
WAIT_SECONDS = 10


class _Blocking:
    """A model that answers only once it is released, and says when it was called."""

    def __init__(self) -> None:
        self.called, self.release = threading.Event(), threading.Event()
        self.calls = 0

    def next_turn(self, prompt: Prompt, *, timeout_ms: int) -> ModelTurn:
        self.calls += 1
        self.called.set()
        self.release.wait(WAIT_SECONDS)
        return ScriptedModel([{"final": "OK"}]).next_turn(prompt, timeout_ms=timeout_ms)


class _TakesOverWhileItRuns:
    """A read adapter during whose call an operator takes the conversation."""

    def __init__(self, adapter: ToolAdapter, database: Database, auth: AuthContext) -> None:
        self.tenant_id = adapter.tenant_id
        self._adapter, self._database, self._auth = adapter, database, auth

    def __call__(self, arguments: BaseModel, *, conversation_id: str) -> Mapping[str, object]:
        self._database.for_tenant(self._auth).chat.apply_takeover(conversation_id, 1, "operador_a")
        return self._adapter(arguments, conversation_id=conversation_id)


class _Rig:
    def __init__(self, workdir: Path, data: HarnessData) -> None:
        self.data = data
        self.auth = data.directory.authenticate("comercio_a", "operador_a")
        self.database = Database(workdir / "cortex.db", CLOCK)
        self.database.initialize()
        self.binder = zoco_tool_binder(data, ClaimsDesk(workdir / "claims", CLOCK), clock=CLOCK)

    def runner(self, model: ModelPort, binder: ToolBinder | None = None) -> AgentRunner:
        return build_runner(
            data=self.data,
            database=self.database,
            model=model,
            bind_tools=binder or self.binder,
            clock=CLOCK,
            ticker=ManualTicker(),
        )

    def request(self, run_id: str, **limits: int) -> RunRequest:
        return RunRequest(
            run_id,
            "conv_1",
            "¿Cuánto vendí el 15?",
            self.auth,
            self.data.config.limits.with_limits(limits),
        )


@pytest.fixture
def rig(tmp_path: Path, harness_data: HarnessData) -> _Rig:
    return _Rig(tmp_path, harness_data)


def test_a_second_run_on_a_busy_conversation_is_refused_without_calling_the_model(
    rig: _Rig,
) -> None:
    first_model, second_model = _Blocking(), ScriptedModel([{"final": "OK"}])
    results: dict[str, RunResult] = {}
    first = threading.Thread(
        target=lambda: results.update(first=rig.runner(first_model).run(rig.request("run-1")))
    )
    first.start()
    try:
        assert first_model.called.wait(WAIT_SECONDS), "the first run never reached the model"

        second = rig.runner(second_model).run(rig.request("run-2"))
    finally:
        first_model.release.set()
        first.join(WAIT_SECONDS)

    assert (second.finish_reason, second.finish_detail) == (
        FinishReason.BLOCKED,
        FinishDetail.CONVERSATION_BUSY,
    )
    assert second_model.prompts == []
    assert results["first"].finish_detail is FinishDetail.ANSWERED
    assert first_model.calls == 1


def test_a_takeover_while_a_read_tool_runs_stops_the_run_before_the_next_model_call(
    rig: _Rig,
) -> None:
    def bind(auth: AuthContext) -> Mapping[str, ToolAdapter]:
        adapters = dict(rig.binder(auth))
        adapters[CONSULTAR_VENTAS] = _TakesOverWhileItRuns(
            adapters[CONSULTAR_VENTAS], rig.database, auth
        )
        return adapters

    model = ScriptedModel([READ_SALES, {"final": "15 ventas"}])

    result = rig.runner(model, bind).run(rig.request("run-1"))

    assert len(model.prompts) == 1  # checkpoint A: the model is not asked again
    assert (result.finish_reason, result.finish_detail) == (
        FinishReason.NEEDS_HUMAN,
        FinishDetail.OPERATOR_TOOK_OVER,
    )
    assert "checkpoint_failed" in [event.type for event in result.events]


def test_a_model_slower_than_the_time_left_ends_the_run_at_its_total_time(rig: _Rig) -> None:
    model = _Blocking()
    try:
        result = rig.runner(model).run(rig.request("run-1", total_timeout_ms=50))
    finally:
        model.release.set()

    assert (result.finish_reason, result.finish_detail) == (
        FinishReason.LIMIT_REACHED,
        FinishDetail.TOTAL_TIMEOUT,
    )
    failed: list[Any] = [e.attributes for e in result.events if e.type == "model_failed"]
    assert failed == [{"code": "total_timeout"}]
