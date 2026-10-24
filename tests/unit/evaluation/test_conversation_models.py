"""The model modes, the call recorder, the prices and the live limits."""

from collections.abc import Sequence
from decimal import Decimal

import pytest
from evaluation.conversations.models import (
    NULL_SENTENCE,
    Budget,
    BudgetExhausted,
    ConversationTimeLimit,
    LiveLimits,
    NullModel,
    ObservedModel,
    ReactiveModel,
    UnknownCost,
)
from evaluation.conversations.pricing import cost, family, is_same_model, price_for
from evaluation.conversations.script import View, by_user_text, data_of, passages

from merchant_assistant.agent.domain.messages import Message, ModelReply, Role
from merchant_assistant.agent.domain.tools import TOOL_SPECS, ToolSpec
from merchant_assistant.agent.ports import Completion

TOOLS = tuple(TOOL_SPECS.values())
PROMPT = Message(Role.SYSTEM, "You are the assistant.")


def ask(text: str) -> tuple[Message, ...]:
    return (PROMPT, Message(Role.USER, text))


def searching(view: View) -> ModelReply:
    if view.result_of("buscar_documentacion") is None:
        return view.call(("buscar_documentacion", {"consulta": view.last_user}))
    return view.say("listo")


def test_a_reactive_model_numbers_its_calls_per_instance() -> None:
    first, second = ReactiveModel(searching), ReactiveModel(searching)

    ids = [m.complete(ask("¿Cuánto dura?"), TOOLS).reply.tool_calls[0].id for m in (first, second)]

    assert ids == ["call-1", "call-1"]
    again = first.complete(ask("otra"), TOOLS)
    assert again.reply.tool_calls[0].id == "call-2"
    assert (again.model, again.prompt_tokens) == ("scripted-model", None)


def test_the_view_reads_only_the_results_of_the_current_turn() -> None:
    envelope = (
        '{"tool": "buscar_documentacion", "status": "ok", '
        '"data": {"passages": [{"doc_id": "DOC-04"}]}}'
    )
    history = (
        PROMPT,
        Message(Role.USER, "primera"),
        Message(Role.TOOL, envelope, tool_call_id="c1"),
        Message(Role.USER, "segunda"),
    )
    view = View(history, ("buscar_documentacion",), lambda: "id")

    assert view.results() == ()
    assert view.user_texts == ("primera", "segunda")
    assert view.system_prompt == PROMPT.content
    turn = View(history[:3], (), lambda: "id")
    assert passages(turn.result_of("buscar_documentacion")) == [{"doc_id": "DOC-04"}]
    assert data_of(None) == {}


def test_rules_match_the_folded_user_text_in_order() -> None:
    policy = by_user_text(
        ("cambia el motivo", lambda v: v.say("cambio")),
        (r"\bsi\b", lambda v: v.say("si")),
        default=lambda v: v.say("otro"),
    )
    view = View(ask("Sí, pero CAMBIÁ el motivo"), (), lambda: "id")
    assert policy(view).text == "cambio"
    assert policy(View(ask("sí"), (), lambda: "id")).text == "si"
    assert policy(View(ask("hola"), (), lambda: "id")).text == "otro"


def test_the_null_model_never_calls_a_tool() -> None:
    reply = NullModel().complete(ask("registrá mi reclamo ya"), TOOLS).reply
    assert (reply.text, reply.tool_calls) == (NULL_SENTENCE, ())


class Priced:
    def __init__(self, served: str = "gpt-4o-mini-2024-07-18") -> None:
        self.served = served

    def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> Completion:
        return Completion(ModelReply("hola", ()), self.served, 1_000_000, 100_000)


def test_the_recorder_keeps_served_models_usage_prompts_and_latency() -> None:
    ticks = iter([1.0, 1.5, 2.0, 2.25])
    model = ObservedModel(Priced(), timer=lambda: next(ticks))

    model.complete(ask("a"), TOOLS)
    model.complete(ask("b"), TOOLS)

    assert [(c.served_model, c.prompt_tokens, c.latency_seconds) for c in model.calls] == [
        ("gpt-4o-mini-2024-07-18", 1_000_000, 0.5),
        ("gpt-4o-mini-2024-07-18", 1_000_000, 0.25),
    ]
    assert model.prompts == [PROMPT.content]


def test_the_budget_stops_the_next_call_once_the_cap_is_reached() -> None:
    budget = Budget(Decimal("0.20"))
    limits = LiveLimits(budget, 60.0, lambda: 0.0)
    model = ObservedModel(Priced(), timer=lambda: 0.0, limits=limits)

    model.complete(ask("a"), TOOLS)  # 0.15 + 0.06 = 0.21 USD

    assert budget.spent_usd == Decimal("0.21")
    with pytest.raises(BudgetExhausted):
        model.complete(ask("b"), TOOLS)
    assert len(model.calls) == 1


def test_a_model_without_a_price_stops_the_run_instead_of_counting_as_free() -> None:
    budget = Budget(Decimal(1))
    model = ObservedModel(
        Priced("unknown-model"), timer=lambda: 0.0, limits=LiveLimits(budget, 60.0, lambda: 0.0)
    )
    with pytest.raises(UnknownCost):
        model.complete(ask("a"), TOOLS)
    assert (budget.spent_usd, budget.unknown_calls) == (Decimal(0), 1)


def test_a_conversation_over_its_ceiling_makes_no_further_call() -> None:
    now = [0.0]
    limits = LiveLimits(Budget(Decimal(1)), 10.0, lambda: now[0])
    model = ObservedModel(Priced(), timer=lambda: now[0], limits=limits)
    model.complete(ask("a"), TOOLS)
    now[0] = 11.0
    with pytest.raises(ConversationTimeLimit):
        model.complete(ask("b"), TOOLS)


def test_a_budget_must_be_positive() -> None:
    with pytest.raises(ValueError, match="above 0"):
        Budget(Decimal(0))


def test_prices_cover_dated_snapshots_and_say_unknown_otherwise() -> None:
    assert family("gpt-4o-mini-2024-07-18") == "gpt-4o-mini"
    assert price_for("gpt-4o-mini-2024-07-18") == price_for("gpt-4o-mini")
    assert price_for("gpt-4.1") is None
    assert cost("gpt-4o-mini", 2_000_000, 1_000_000) == Decimal("0.90")
    assert cost("gpt-4o-mini", None, 10) is None
    assert cost("other", 10, 10) is None
    assert is_same_model("gpt-4o-mini", "gpt-4o-mini-2024-07-18")
    assert not is_same_model("gpt-4o-mini", "gpt-4o")
