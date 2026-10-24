"""The real chat endpoint. Deselected by default; run on purpose with `pytest -m network`."""

import pytest

from merchant_assistant.agent.domain.messages import Message, Role
from merchant_assistant.agent.domain.tools import TOOL_SPECS
from merchant_assistant.bootstrap import build_chat_model
from merchant_assistant.shared.config import Settings

pytestmark = pytest.mark.network


def test_the_real_endpoint_answers_and_proposes_a_tool_call_for_a_documentation_question() -> None:
    settings = Settings()
    if settings.effective_llm_api_key() is None:
        pytest.skip("LLM_API_KEY (or OPENAI_API_KEY) is not configured")
    model = build_chat_model(settings)

    plain = model.complete(
        [Message(Role.USER, "Respondé solo con la palabra OK.")],
        (),
    )
    with_tools = model.complete(
        [
            Message(Role.SYSTEM, "Use the tools for facts about the documentation."),
            Message(Role.USER, "¿Cuánto dura la reserva del carrito?"),
        ],
        (TOOL_SPECS["buscar_documentacion"],),
    )

    assert plain.reply.text
    assert plain.model
    assert with_tools.reply.tool_calls or with_tools.reply.text
