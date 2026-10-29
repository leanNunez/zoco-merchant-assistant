"""What the model receives: bounded history, structured state, results marked as untrusted."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cortex.bootstrap import HarnessData, build_runner, zoco_tool_binder
from cortex.memory.database import Database
from cortex.runner.agent_runner import RunRequest
from cortex.runner.budget import ManualTicker
from cortex.runner.finish import FinishReason
from cortex.runner.scripted_model import ScriptedModel
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
        bind_tools=zoco_tool_binder(harness_data),
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
    assert second_prompt.state == {"conversation_mode": "AUTO", "control_version": 0}
