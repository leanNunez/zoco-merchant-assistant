"""HARNESS ONLY. A deliberately UNSAFE agent: the evaluator's positive control.

Nothing in `src/` imports this module (a test walks the package to prove it), and it must stay
that way. It exists to answer one question about the evaluation itself: when an agent DOES break
the policies, do the checks notice? If a LOCK check passed with this agent on an attack scenario,
the check would be measuring nothing.

It is what an agent looks like without the code's guards: it offers every tool in every turn,
matches tool names loosely (case and spaces ignored), ignores unexpected arguments such as
`confirmado: true`, executes every call directly through the same capability ports the real
agent uses (including `crear_reclamo`, with no confirmation and no pending action), has no
control machine (no AUTO, SUGGEST, HUMAN or BLOCKED, so the team's commands do nothing), no
output checks, and returns whatever the model writes. It loops up to four model calls per
message, like the real turn.
"""

from __future__ import annotations

import json
from datetime import tzinfo

from evaluation.conversations.drivers import Reaction, Snapshot
from evaluation.conversations.observation import HistoryEntry
from evaluation.conversations.scenario import Team
from evaluation.conversations.world import Ground, World
from merchant_assistant.agent.application.conversation import ToolRecord
from merchant_assistant.agent.application.executor import envelope
from merchant_assistant.agent.application.prompts import describe_hours, system_prompt
from merchant_assistant.agent.domain.messages import Message, Role, ToolCall
from merchant_assistant.agent.domain.tools import TOOL_SPECS
from merchant_assistant.agent.ports import (
    ChatModel,
    ComplaintRegistry,
    KnowledgeSearch,
    SalesLookup,
)
from merchant_assistant.bootstrap import ComplaintsAdapter, SalesAdapter, build_search
from merchant_assistant.complaints.adapters.faulty_repository import FaultyComplaintRepository
from merchant_assistant.complaints.adapters.sqlite_repository import SqliteComplaintRepository
from merchant_assistant.complaints.service import ComplaintService
from merchant_assistant.control import BusinessHours
from merchant_assistant.sales.adapters.faulty_repository import FaultySalesRepository
from merchant_assistant.sales.adapters.json_repository import JsonSalesRepository
from merchant_assistant.sales.service import SalesService
from merchant_assistant.shared.clock import Clock, business_today
from merchant_assistant.shared.errors import DomainError

MAX_ROUNDS = 4
NOT_SUPPORTED = "not_supported"
ROUND_LIMIT_TEXT = "No pude completar tu consulta."
MODEL_FAILED_TEXT = "No pude procesar tu mensaje."
_SPECS = tuple(TOOL_SPECS.values())


class NaiveAgent:
    def __init__(
        self,
        model: ChatModel,
        *,
        search: KnowledgeSearch,
        sales: SalesLookup,
        complaints: ComplaintRegistry,
        clock: Clock,
        hours: BusinessHours,
        conversation_id: str,
    ) -> None:
        self._model = model
        self._search = search
        self._sales = sales
        self._complaints = complaints
        self._clock = clock
        self._hours = hours
        self._cid = conversation_id
        self._messages: list[Message] = []

    def say(self, text: str) -> Reaction:
        self._messages.append(Message(Role.USER, text))
        prompt = Message(Role.SYSTEM, self._prompt())
        records: list[ToolRecord] = []
        calls = 0
        for _ in range(MAX_ROUNDS):
            calls += 1
            try:
                reply = self._model.complete((prompt, *self._messages), _SPECS).reply
            except DomainError as failure:
                return self._answer(MODEL_FAILED_TEXT, records, calls, model_error=failure.code)
            if not reply.tool_calls:
                return self._answer(reply.text or "", records, calls)
            self._messages.append(Message(Role.ASSISTANT, None, reply.tool_calls))
            for call in reply.tool_calls:
                content, record = self._execute(call)
                records.append(record)
                self._messages.append(Message(Role.TOOL, content, tool_call_id=call.id))
        return self._answer(ROUND_LIMIT_TEXT, records, calls)

    def command(self, step: Team) -> Reaction:
        return Reaction(error_code=NOT_SUPPORTED)

    def snapshot(self) -> Snapshot:
        history = tuple(
            HistoryEntry(
                role=str(m.role),
                author=str(m.author),
                content=m.content,
                tool_calls=tuple((c.id, c.name, c.arguments) for c in m.tool_calls),
                tool_call_id=m.tool_call_id,
            )
            for m in self._messages
        )
        return Snapshot(history=history, final_state=None)

    # ------------------------------------------------------------------ internals

    def _prompt(self) -> str:
        tz: tzinfo = self._hours.tz
        zone = getattr(tz, "key", None) or str(tz)
        today = business_today(self._clock, tz)
        return system_prompt(today, zone, describe_hours(self._hours), TOOL_SPECS.keys())

    def _answer(
        self, text: str, records: list[ToolRecord], calls: int, *, model_error: str | None = None
    ) -> Reaction:
        shown = text.strip() or ROUND_LIMIT_TEXT
        self._messages.append(Message(Role.ASSISTANT, shown))
        return Reaction(
            visible=(("reply", shown),),
            tools=tuple(records),
            model_calls=calls,
            model_error=model_error,
        )

    def _execute(self, call: ToolCall) -> tuple[str, ToolRecord]:
        name = " ".join(call.name.split()).lower()
        try:
            parsed = json.loads(call.arguments)
        except ValueError:
            parsed = {}
        arguments = parsed if isinstance(parsed, dict) else {}
        try:
            status, data = self._dispatch(name, arguments)
        except DomainError as failure:
            content = envelope(name, "error", error={"code": failure.code})
            return content, ToolRecord(
                call.name, call.arguments, "executed", "error", 1, failure.code
            )
        if status == "unknown":
            content = envelope(name, "error", error={"code": "unknown_tool"})
            return content, ToolRecord(
                call.name, call.arguments, "rejected", None, 0, "unknown_tool"
            )
        content = envelope(name, "ok" if status == "ok" else "no_data", data=data)
        return content, ToolRecord(call.name, call.arguments, "executed", status, 1)

    def _dispatch(self, name: str, arguments: dict[str, object]) -> tuple[str, dict[str, object]]:
        if name == "buscar_documentacion":
            outcome = self._search.search(str(arguments.get("consulta", "")))
            passages = [
                {
                    "doc_id": p.doc_id,
                    "valid_from": p.valid_from.isoformat(),
                    "valid_until": p.valid_until.isoformat(),
                    "text": p.text,
                }
                for p in outcome.passages
            ]
            return outcome.status, {"passages": passages}
        if name == "consultar_ventas":
            found = self._sales.lookup(str(arguments.get("fecha", "")))
            if found.figures is None:
                return "no_data", {"fecha": found.day.isoformat(), "registered": False}
            figures = found.figures
            return "ok", {
                "fecha": found.day.isoformat(),
                "registered": True,
                "entradas": figures.tickets,
                "bruto": str(figures.gross),
                "devoluciones": str(figures.refunds),
                "neto": str(figures.net),
            }
        if name == "crear_reclamo":
            registration = self._complaints.register(
                str(arguments.get("motivo", "")), str(arguments.get("descripcion", "")), self._cid
            )
            return "ok", {"registered": True, "complaint_id": registration.complaint_id}
        return "unknown", {}


def build_naive(ground: Ground, model: ChatModel, conversation_id: str) -> World:
    """The naive agent over the same capabilities, injectors and files a real world uses."""
    settings, clock = ground.settings, ground.clock
    tz = settings.timezone
    search = build_search(
        settings, today=lambda: business_today(clock, tz), docs_dir=ground.docs_dir
    )
    sales_repository = FaultySalesRepository(
        JsonSalesRepository(settings.sales_path), ground.faults["consultar_ventas"]
    )
    store = SqliteComplaintRepository(settings.complaints_db_path)
    store.initialize()
    complaints = FaultyComplaintRepository(store, ground.faults["crear_reclamo"])
    agent = NaiveAgent(
        model,
        search=search.search,
        sales=SalesAdapter(SalesService(sales_repository, clock, tz)),
        complaints=ComplaintsAdapter(ComplaintService(complaints, clock)),
        clock=clock,
        hours=ground.hours,
        conversation_id=conversation_id,
    )
    return World(agent, ground, search.mode, search.reason)
