"""What one run of a scenario left behind, as plain facts: the only thing checks may look at.

Every field is something an outside observer can verify: which tools ran with which arguments,
what reached the customer, which rows are in the SQLite file, the control log and the final state.
Nothing here is an opinion about the agent's prose. Drafts are kept apart from what the customer
saw, because in SUGGEST a draft has not been sent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal

StepKind = Literal["user", "team", "clock", "faults"]
VisibleKind = Literal["reply", "notice", "operator"]


@dataclass(frozen=True, slots=True)
class ToolFact:
    step: int
    name: str
    arguments: str  # the raw JSON text the model produced
    outcome: str  # executed, rejected, proposed, ignored, deduplicated, awaiting_operator
    status: str | None = None
    attempts: int = 0
    error_code: str | None = None

    def argument(self, key: str) -> object:
        """One argument as the model sent it; None when absent or not a JSON object."""
        try:
            parsed = json.loads(self.arguments)
        except ValueError:
            return None
        return parsed.get(key) if isinstance(parsed, dict) else None


@dataclass(frozen=True, slots=True)
class Visible:
    """A message that reached the customer: an answer, a code notice or an operator's text."""

    step: int
    kind: VisibleKind
    text: str


@dataclass(frozen=True, slots=True)
class DraftFact:
    step: int  # the step that created it
    id: str
    kind: str  # reply, confirmation_request, action_approval
    text: str
    status: str = "pending"  # as stored at the end of the run


@dataclass(frozen=True, slots=True)
class StepFact:
    index: int
    tag: str | None
    kind: StepKind
    detail: str  # the user's text, the command, the new instant or the fault plan
    skipped: bool = False  # a conditional user reply whose condition did not hold
    state_after: str | None = None  # None: the agent has no control machine (naive)
    rows_after: int = 0
    visible: tuple[Visible, ...] = ()
    drafts: tuple[str, ...] = ()
    events: tuple[str, ...] = ()
    violations: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()
    model_calls: int = 0
    gate: str | None = None
    model_error: str | None = None
    error_code: str | None = None  # a refused team command
    injection_markers: int = 0


@dataclass(frozen=True, slots=True)
class RowFact:
    id: str
    reason: str
    description: str
    conversation_id: str
    created_at: str


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    role: str
    author: str
    content: str | None
    tool_calls: tuple[tuple[str, str, str], ...] = ()  # (id, name, raw arguments)
    tool_call_id: str | None = None


@dataclass(frozen=True, slots=True)
class LogEntry:
    previous: str
    new: str
    event: str
    actor: str
    outcome: str
    reason: str | None
    at: str


@dataclass(frozen=True, slots=True)
class InboxEntry:
    code: str
    detail: str
    at: str


@dataclass(frozen=True, slots=True)
class ModelCall:
    served_model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_seconds: float | None = None  # measured in live mode only


@dataclass(frozen=True, slots=True)
class SearchResult:
    query: str
    status: str
    doc_ids: tuple[str, ...]


@dataclass(frozen=True)
class Observation:
    steps: tuple[StepFact, ...] = ()
    tools: tuple[ToolFact, ...] = ()
    rows: tuple[RowFact, ...] = ()
    history: tuple[HistoryEntry, ...] = ()
    control_log: tuple[LogEntry, ...] = ()
    inbox: tuple[InboxEntry, ...] = ()
    drafts: tuple[DraftFact, ...] = ()
    final_state: str | None = None
    model_calls: tuple[ModelCall, ...] = ()
    # What the model was told (to detect a leak). Kept out of the saved results: only its hash.
    system_prompts: tuple[str, ...] = field(default=(), repr=False)
    # Text the merchant shows customers on purpose that the prompt also contains (the hours).
    public_texts: tuple[str, ...] = ()

    @property
    def visible(self) -> tuple[Visible, ...]:
        return tuple(message for step in self.steps for message in step.visible)

    def step(self, tag: str) -> StepFact:
        for step in self.steps:
            if step.tag == tag:
                return step
        raise KeyError(f"no step tagged {tag!r}")

    def index_of(self, tag: str) -> int:
        return self.step(tag).index

    def search_results(self) -> tuple[SearchResult, ...]:
        """Every documentation search in the stored history, in order, with its returned docs.

        Read from the tool envelopes themselves (what the model was given), paired with the
        query of the call they answer.
        """
        queries: dict[str, str] = {}
        found: list[SearchResult] = []
        for entry in self.history:
            for call_id, name, arguments in entry.tool_calls:
                if name == "buscar_documentacion":
                    queries[call_id] = _query_of(arguments)
            if entry.role != "tool" or entry.content is None or entry.tool_call_id not in queries:
                continue
            try:
                payload = json.loads(entry.content)
            except ValueError:
                continue
            if not isinstance(payload, dict) or payload.get("tool") != "buscar_documentacion":
                continue
            data = payload.get("data")
            raw = data.get("passages", []) if isinstance(data, dict) else []
            ids = tuple(str(p.get("doc_id")) for p in raw if isinstance(p, dict))
            found.append(SearchResult(queries[entry.tool_call_id], str(payload.get("status")), ids))
        return tuple(found)


def _query_of(arguments: str) -> str:
    try:
        parsed = json.loads(arguments)
    except ValueError:
        return ""
    value = parsed.get("consulta") if isinstance(parsed, dict) else None
    return value if isinstance(value, str) else ""
