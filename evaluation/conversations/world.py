"""One isolated world per (scenario, repetition), built by the real composition root.

Everything the agent touches is the production code: `build_agent_service` wires the real
`ConversationService` and `OperatorService` over one in-memory store, the SQLite complaints
repository (a fresh file in the run's temporary directory), `JsonSalesRepository` over the
committed `data/ventas.json`, and the real search over the corpus. Only four things are
substituted, all through parameters the composition root already takes: the model, the clock
(one the harness can move), the sleeper (it never really sleeps) and the fault injectors.

Nothing is shared between worlds: a new directory, database, store, clock and injectors every
time, and the settings are built here from explicit values, never from the process-wide cached
`get_settings()`. In the offline modes no key is used, so retrieval runs in its degraded mode
(BM25 with the validity policy) whatever the environment says.
"""

from __future__ import annotations

import shutil
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path

from evaluation.conversations.drivers import AgentUnderTest, RealAgent
from evaluation.conversations.observation import RowFact
from evaluation.conversations.scenario import CorpusPatch, FaultPlan, WorldSpec
from evaluation.retrieval.provenance import corpus_digest
from merchant_assistant.agent.adapters.memory_store import InMemoryConversationStore
from merchant_assistant.agent.application.prompts import describe_hours
from merchant_assistant.agent.ports import ChatModel
from merchant_assistant.bootstrap import SearchMode, build_agent_service, build_business_hours
from merchant_assistant.control import BusinessHours
from merchant_assistant.shared.config import Settings
from merchant_assistant.shared.faults import FaultConfig, FaultInjector, FaultMode

REPOSITORY = Path(__file__).resolve().parents[2]
DOCS = REPOSITORY / "docs"
SALES = REPOSITORY / "data" / "ventas.json"

# The world's business configuration is part of the scenarios (X4 sends a message at 22:00 and
# expects the window to be closed), so it is fixed here instead of read from the environment.
# These are the defaults of `Settings`.
WORLD_TIMEZONE = "America/Argentina/Buenos_Aires"
WORLD_OPENS, WORLD_CLOSES = time(9, 0), time(18, 0)
WORLD_DAYS = "mon,tue,wed,thu,fri"


@dataclass
class MovableClock:
    instant: datetime

    def __post_init__(self) -> None:
        if self.instant.utcoffset() is None:
            raise ValueError("the clock needs a timezone-aware instant")

    def now(self) -> datetime:
        return self.instant

    def move_to(self, instant: datetime) -> None:
        if instant.utcoffset() is None:
            raise ValueError("the clock needs a timezone-aware instant")
        self.instant = instant


class ScriptedFaults(FaultInjector):
    """ "Fail the first N calls with MODE, then succeed", deterministically.

    Without a plan it defers to `fallback`, the seeded rate-based injector of the settings, so
    the rate-driven failures of production stay available. A new plan starts counting again.
    """

    def __init__(self, plan: FaultPlan | None, fallback: FaultInjector) -> None:
        super().__init__(FaultConfig.off())
        self._plan = plan
        self._fallback = fallback
        self._calls = 0
        self.injected: list[str] = []

    def set_plan(self, plan: FaultPlan | None) -> None:
        self._plan = plan
        self._calls = 0

    def next_fault(self, supports: frozenset[FaultMode]) -> FaultMode | None:
        if self._plan is None:
            return self._fallback.next_fault(supports)
        self._calls += 1
        limit = self._plan.first_calls
        if self._plan.mode not in supports or (limit is not None and self._calls > limit):
            return None
        self.injected.append(str(self._plan.mode))
        return self._plan.mode


@dataclass(frozen=True)
class Ground:
    """What a world is built on: settings, clock, injectors and the corpus it reads."""

    settings: Settings
    clock: MovableClock
    faults: dict[str, ScriptedFaults]
    docs_dir: Path
    corpus_sha256: str
    hours: BusinessHours
    seed: int

    @property
    def db_path(self) -> Path:
        return self.settings.complaints_db_path

    @property
    def public_texts(self) -> tuple[str, ...]:
        """What the merchant tells customers on purpose and the prompt also says: the hours."""
        return (describe_hours(self.hours),)


@dataclass
class World:
    agent: AgentUnderTest
    ground: Ground
    search_mode: SearchMode
    search_reason: str
    sleeps: list[float] = field(default_factory=list)

    def move_clock(self, instant: datetime) -> None:
        self.ground.clock.move_to(instant)

    def set_faults(self, tool: str, plan: FaultPlan | None) -> None:
        self.ground.faults[tool].set_plan(plan)

    def count_rows(self) -> int:
        return count_rows(self.ground.db_path)

    def read_rows(self) -> tuple[RowFact, ...]:
        return read_rows(self.ground.db_path)


def offline_settings(directory: Path, docs_dir: Path) -> Settings:
    """Settings for the scripted, null and naive modes: no key, no environment, no `.env`."""
    return Settings(
        _env_file=None,
        openai_api_key=None,
        llm_api_key=None,
        tool_failure_rate=0.0,
        tool_failure_seed=0,
        sales_path=SALES,
        complaints_db_path=directory / "reclamos.db",
        docs_dir=docs_dir,
        embeddings_cache_path=directory / "embeddings.sqlite",
        business_timezone=WORLD_TIMEZONE,
        human_hours_start=WORLD_OPENS,
        human_hours_end=WORLD_CLOSES,
        human_hours_days=WORLD_DAYS,
    )


def live_settings(base: Settings, directory: Path, docs_dir: Path) -> Settings:
    """The configured settings (model, key, retrieval) with this world's files and hours."""
    return base.model_copy(
        update={
            "tool_failure_rate": 0.0,
            "sales_path": SALES,
            "complaints_db_path": directory / "reclamos.db",
            "docs_dir": docs_dir,
            "business_timezone": WORLD_TIMEZONE,
            "human_hours_start": WORLD_OPENS,
            "human_hours_end": WORLD_CLOSES,
            "human_hours_days": WORLD_DAYS,
        }
    )


def prepare(spec: WorldSpec, directory: Path, *, seed: int, base: Settings | None = None) -> Ground:
    """The ground of one world in `directory` (which must be empty and private to it)."""
    docs_dir = DOCS if spec.corpus_patch is None else _poisoned_copy(spec.corpus_patch, directory)
    settings = (
        offline_settings(directory, docs_dir)
        if base is None
        else live_settings(base, directory, docs_dir)
    )
    fallback = FaultConfig(rate=0.0, seed=seed, modes=frozenset())
    faults = {
        "consultar_ventas": ScriptedFaults(spec.sales_faults, FaultInjector(fallback)),
        "crear_reclamo": ScriptedFaults(spec.complaint_faults, FaultInjector(fallback)),
    }
    return Ground(
        settings=settings,
        clock=MovableClock(spec.start),
        faults=faults,
        docs_dir=docs_dir,
        corpus_sha256=docs_digest(docs_dir),
        hours=build_business_hours(settings),
        seed=seed,
    )


def build_real(ground: Ground, model: ChatModel, conversation_id: str) -> World:
    """The production agent over this ground."""
    store = InMemoryConversationStore()
    sleeps: list[float] = []
    built = build_agent_service(
        ground.settings,
        clock=ground.clock,
        model=model,
        store=store,
        sleeper=sleeps.append,
        retry_seed=ground.seed,
        sales_faults=ground.faults["consultar_ventas"],
        complaint_faults=ground.faults["crear_reclamo"],
    )
    agent = RealAgent(built.service, built.operators, store, conversation_id)
    return World(agent, ground, built.search_mode, built.search_reason, sleeps)


def docs_digest(docs_dir: Path) -> str:
    files = [(f"docs/{p.name}", p.read_bytes()) for p in sorted(docs_dir.glob("DOC-*.md"))]
    return corpus_digest(files)


def count_rows(db_path: Path) -> int:
    if not db_path.exists():
        return 0
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as connection:
        return int(connection.execute("SELECT COUNT(*) FROM complaints").fetchone()[0])


def read_rows(db_path: Path) -> tuple[RowFact, ...]:
    """The complaints exactly as stored, in creation order, read straight from the file."""
    if not db_path.exists():
        return ()
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as connection:
        rows = connection.execute(
            "SELECT seq, reason, description, conversation_id, created_at "
            "FROM complaints ORDER BY seq"
        ).fetchall()
    return tuple(
        RowFact(f"REC-{seq:06d}", reason, description, conversation_id, created_at)
        for seq, reason, description, conversation_id, created_at in rows
    )


def _poisoned_copy(patch: CorpusPatch, directory: Path) -> Path:
    """A copy of the corpus in `directory`, with `patch.addition` appended to one document."""
    target = directory / "docs"
    target.mkdir()
    for source in sorted(DOCS.glob("DOC-*.md")):
        shutil.copyfile(source, target / source.name)
    poisoned = target / f"{patch.document}.md"
    if not poisoned.exists():
        raise ValueError(f"the corpus has no {patch.document}")
    text = poisoned.read_text(encoding="utf-8").rstrip("\n")
    poisoned.write_text(f"{text}\n\n{patch.addition.strip()}\n", encoding="utf-8")
    return target
