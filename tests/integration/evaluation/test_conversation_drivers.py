"""The driver plays steps against the real services and writes down only facts.

The agent here is the production composition (`build_agent_service`) over the real corpus, the
real sales file and a SQLite file in a temporary directory, with a hand-scripted model.
"""

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest
from evaluation.conversations.drivers import NO_MATCHING_DRAFT, RealAgent, drive, observe
from evaluation.conversations.observation import RowFact
from evaluation.conversations.scenario import (
    FaultPlan,
    MoveClock,
    Say,
    SetFaults,
    Step,
    Team,
    TeamCommand,
    When,
)

from merchant_assistant.agent.adapters.memory_store import InMemoryConversationStore
from merchant_assistant.agent.adapters.scripted_model import (
    ScriptedChatModel,
    ScriptStep,
    calls_reply,
    text_reply,
    tool_call,
)
from merchant_assistant.agent.domain.drafts import DraftKind
from merchant_assistant.bootstrap import build_agent_service
from merchant_assistant.control import Actor
from merchant_assistant.shared.config import Settings
from merchant_assistant.shared.faults import FaultMode

pytestmark = pytest.mark.integration

REPOSITORY = Path(__file__).resolve().parents[3]
NOON = datetime(2026, 10, 23, 15, 0, tzinfo=UTC)
A = {"motivo": "Entrada no emitida", "descripcion": "Pagué la entrada y no me llegó el correo."}


@dataclass
class Clock:
    instant: datetime

    def now(self) -> datetime:
        return self.instant


@dataclass
class Controls:
    clock: Clock
    db: Path
    plans: list[tuple[str, FaultPlan | None]] = field(default_factory=list)

    def move_clock(self, instant: datetime) -> None:
        self.clock.instant = instant

    def set_faults(self, tool: str, plan: FaultPlan | None) -> None:
        self.plans.append((tool, plan))

    def count_rows(self) -> int:
        with sqlite3.connect(self.db) as connection:
            return int(connection.execute("SELECT COUNT(*) FROM complaints").fetchone()[0])


def world(tmp_path: Path, steps: list[ScriptStep]) -> tuple[RealAgent, Controls]:
    db = tmp_path / "reclamos.db"
    settings = Settings(
        _env_file=None,
        openai_api_key=None,
        complaints_db_path=db,
        sales_path=REPOSITORY / "data" / "ventas.json",
        docs_dir=REPOSITORY / "docs",
    )
    clock = Clock(NOON)
    store = InMemoryConversationStore()
    built = build_agent_service(
        settings, clock=clock, model=ScriptedChatModel(steps), store=store, sleeper=lambda _: None
    )
    return RealAgent(built.service, built.operators, store, "c1"), Controls(clock, db)


def test_a_complaint_flow_leaves_tools_visible_messages_rows_and_state(tmp_path: Path) -> None:
    agent, controls = world(tmp_path, [calls_reply(tool_call("crear_reclamo", A))])
    steps: list[Step] = [
        Say("sí", tag="early"),
        Say("Quiero reclamar: no me llegó la entrada.", tag="details"),
        Say("sí", tag="yes", when=When.CONFIRMATION_REQUESTED),
    ]

    driven = drive(steps, agent, controls)

    early, details, yes = driven.steps
    assert (early.gate, early.model_calls, early.rows_after) == ("nothing_pending", 0, 0)
    assert [(t.step, t.name, t.outcome) for t in driven.tools] == [
        (1, "crear_reclamo", "proposed"),
        (2, "crear_reclamo", "executed"),
    ]
    assert details.visible[0].text.startswith("Voy a registrar este reclamo:")
    assert (yes.skipped, yes.rows_after, yes.state_after, yes.gate) == (
        False,
        1,
        "AUTO",
        "confirmed",
    )
    assert "REC-000001" in yes.visible[0].text
    assert driven.stopped is None


def test_a_conditional_reply_is_skipped_when_its_question_never_came(tmp_path: Path) -> None:
    agent, controls = world(tmp_path, [text_reply("¿Qué pasó exactamente?")])

    driven = drive(
        [Say("Quiero reclamar."), Say("sí", when=When.CONFIRMATION_REQUESTED)], agent, controls
    )

    assert [(s.kind, s.skipped) for s in driven.steps] == [("user", False), ("user", True)]


def test_team_commands_are_recorded_with_their_refusals(tmp_path: Path) -> None:
    agent, controls = world(tmp_path, [])
    steps: list[Step] = [
        Team(TeamCommand.TAKEOVER, tag="take"),
        Team(TeamCommand.UNBLOCK, tag="unblock"),
        Team(TeamCommand.APPROVE, draft_kind=DraftKind.REPLY, tag="approve"),
        Team(TeamCommand.REPLY, text="Hola, soy Ana.", tag="reply"),
        Team(TeamCommand.RELEASE, actor=Actor.SUPERVISOR, tag="release"),
    ]

    driven = drive([*steps, Say("x", when=When.CONFIRMATION_REQUESTED)], agent, controls)

    by_tag = {s.tag: s for s in driven.steps}
    assert (by_tag["take"].state_after, by_tag["take"].error_code) == ("HUMAN", None)
    assert by_tag["unblock"].error_code == "invalid_transition"
    assert by_tag["approve"].error_code == NO_MATCHING_DRAFT
    assert [(v.kind, v.text) for v in by_tag["reply"].visible] == [("operator", "Hola, soy Ana.")]
    assert by_tag["release"].state_after == "AUTO"
    assert by_tag["take"].detail == "takeover OPERATOR"


def test_clock_and_fault_steps_reach_the_controls(tmp_path: Path) -> None:
    agent, controls = world(tmp_path, [])
    later = datetime(2026, 10, 24, 1, 0, tzinfo=UTC)
    plan = FaultPlan(FaultMode.TIMEOUT, first_calls=2)

    driven = drive(
        [
            MoveClock(later),
            SetFaults("consultar_ventas", plan),
            Say("x", when=When.EXECUTION_FAILED),
        ],
        agent,
        controls,
    )

    assert controls.clock.instant == later
    assert controls.plans == [("consultar_ventas", plan)]
    assert [s.kind for s in driven.steps] == ["clock", "faults", "user"]


def test_a_stop_rule_ends_the_run_and_says_why(tmp_path: Path) -> None:
    agent, controls = world(tmp_path, [text_reply("uno"), text_reply("dos")])

    driven = drive(
        [Say("a"), Say("b")],
        agent,
        controls,
        stop=lambda fact: "provider_unavailable" if fact.index == 0 else None,
    )

    assert (len(driven.steps), driven.stopped) == (1, "provider_unavailable")


def test_observe_adds_the_stored_conversation_and_the_rows(tmp_path: Path) -> None:
    search = calls_reply(
        tool_call("buscar_documentacion", {"consulta": "¿Cuánto dura la reserva del carrito?"})
    )
    agent, controls = world(tmp_path, [search, text_reply("Dura 12 minutos [DOC-04].")])
    driven = drive([Say("¿Cuánto dura la reserva del carrito?")], agent, controls)
    row = RowFact("REC-000001", "m", "d", "c1", "t")

    obs = observe(
        driven, agent.snapshot(), (row,), model_calls=(), system_prompts=(), public_texts=()
    )

    assert obs.final_state == "AUTO"
    assert [h.role for h in obs.history] == ["user", "assistant", "tool", "assistant"]
    assert obs.search_results()[0].doc_ids[0] == "DOC-04"
    assert obs.rows == (row,)
    assert [v.text for v in obs.visible] == ["Dura 12 minutos [DOC-04]."]
