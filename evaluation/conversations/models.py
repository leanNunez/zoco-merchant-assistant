"""The models a scenario can be run with, and the recorder every model call goes through.

- `scripted`: the scenario's own script (`ReactiveModel` over a `script.Policy`): the intended
  behaviour for most cases, a hostile or obedient one for the attacks and state crossings.
- `null`: `NullModel` answers one harmless fixed sentence and never calls a tool. A model that
  does nothing breaks no policy, so it must pass every LOCK check and fail the UTILITY ones.
- `live`: the real chat model of the settings, built by the composition root
  (`bootstrap.build_chat_model`), behind the same recorder.

`ObservedModel` records the served model, the token usage, the latency (live) and the system
prompts of every call. In live mode it also enforces the spending cap and a wall-clock ceiling
per conversation. Both are raised as `AssertionError` subclasses on purpose: the agent's turn
handles every other exception of a model as a provider failure (a safe customer notice), and
re-raises only `AssertionError`, so a harness limit stops the conversation instead of being
mistaken for the model's behaviour.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from evaluation.conversations.observation import ModelCall
from evaluation.conversations.pricing import cost
from evaluation.conversations.script import Policy, View
from merchant_assistant.agent.domain.messages import Message, ModelReply, Role
from merchant_assistant.agent.domain.tools import ToolSpec
from merchant_assistant.agent.ports import ChatModel, Completion

SCRIPTED_MODEL = "scripted-model"
NULL_MODEL = "null-model"
NULL_SENTENCE = "Gracias por tu mensaje."
Timer = Callable[[], float]


class ReactiveModel:
    """A scripted policy as a chat model. Call ids are numbered per instance."""

    def __init__(self, policy: Policy, *, name: str = SCRIPTED_MODEL) -> None:
        self._policy = policy
        self._name = name
        self._ids = itertools.count(1)

    def _next_id(self) -> str:
        return f"call-{next(self._ids)}"

    def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> Completion:
        view = View(tuple(messages), tuple(spec.name for spec in tools), self._next_id)
        return Completion(self._policy(view), self._name, None, None)


class NullModel:
    """Never calls a tool, never fails, never says anything that matters."""

    def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> Completion:
        return Completion(ModelReply(NULL_SENTENCE, ()), NULL_MODEL, None, None)


class BudgetExhausted(AssertionError):  # noqa: N818 - named after the situation
    """The live run reached its spending cap: no further model call is made."""


class ConversationTimeLimit(AssertionError):  # noqa: N818 - named after the situation
    """One live conversation ran longer than its wall-clock ceiling."""


class UnknownCost(AssertionError):  # noqa: N818 - named after the situation
    """A call was served by a model without a price: the cap could no longer be enforced."""


@dataclass
class Budget:
    """The live run's spending, shared by its conversations (never module state)."""

    cap_usd: Decimal
    spent_usd: Decimal = Decimal(0)
    unknown_calls: int = 0

    def __post_init__(self) -> None:
        if not self.cap_usd > 0:
            raise ValueError("the spending cap must be above 0")

    @property
    def exhausted(self) -> bool:
        return self.spent_usd >= self.cap_usd

    def charge(self, model: str, prompt_tokens: int | None, completion_tokens: int | None) -> None:
        price = cost(model, prompt_tokens, completion_tokens)
        if price is None:
            self.unknown_calls += 1
            raise UnknownCost(f"no price or no usage for {model!r}")
        self.spent_usd += price


@dataclass
class LiveLimits:
    budget: Budget
    ceiling_seconds: float
    timer: Timer
    started_at: float = field(init=False)

    def __post_init__(self) -> None:
        self.started_at = self.timer()

    def before_call(self) -> None:
        if self.budget.exhausted:
            raise BudgetExhausted(f"spent {self.budget.spent_usd} of {self.budget.cap_usd} USD")
        if self.timer() - self.started_at > self.ceiling_seconds:
            raise ConversationTimeLimit(f"over {self.ceiling_seconds} s")


class ObservedModel:
    """Any chat model, with every call recorded (and limited, in live mode)."""

    def __init__(
        self, inner: ChatModel, *, timer: Timer | None = None, limits: LiveLimits | None = None
    ) -> None:
        self._inner = inner
        self._timer = timer
        self._limits = limits
        self.calls: list[ModelCall] = []
        self.prompts: list[str] = []

    def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> Completion:
        if self._limits is not None:
            self._limits.before_call()
        for message in messages:
            if message.role is Role.SYSTEM and message.content not in self.prompts:
                self.prompts.append(message.content or "")
        start = None if self._timer is None else self._timer()
        completion = self._inner.complete(messages, tools)
        latency = None if self._timer is None or start is None else self._timer() - start
        self.calls.append(
            ModelCall(
                completion.model,
                completion.prompt_tokens,
                completion.completion_tokens,
                latency,
            )
        )
        if self._limits is not None:
            self._limits.budget.charge(
                completion.model, completion.prompt_tokens, completion.completion_tokens
            )
        return completion
