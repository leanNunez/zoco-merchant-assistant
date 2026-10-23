"""The pre-declared rules that pick a chunking and a winner without looking twice.

With 20 questions it is easy to tune until the numbers look good. These rules are
written down before the tables, applied mechanically, and nothing else is tuned.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from evaluation.retrieval.scoring import StrategyRun
from merchant_assistant.knowledge.chunking import ChunkKind

KIND_ORDER: dict[ChunkKind, int] = {"document": 0, "section": 1, "window": 2}

SELECTION_RULE = (
    "El chunking de la tabla principal es el de mayor MRR híbrido; ante un empate, el de "
    "mayor recall@3 híbrido; luego el de menos chunks; luego el tipo más simple "
    "(documento < sección < ventana) y, por último, el orden de la grilla (sin encabezado "
    "antes que con encabezado, ventanas chicas antes que grandes). Para elegir entre dos "
    "estrategias (el mejor modo de metadata, la mejor estrategia simple) se usa el mayor MRR, "
    "luego el mayor recall@3 y luego un orden fijo declarado de antemano. No se ajusta nada "
    "más sobre estas 20 preguntas."
)

# Exact float ties must count as ties: means of the same values computed in another order
# can differ in the last bit.
_DIGITS = 12


@dataclass(frozen=True, slots=True)
class ChunkingCandidate:
    label: str
    kind: ChunkKind
    chunks: int
    hybrid_mrr: float
    hybrid_recall_at_3: float
    grid_position: int


@dataclass(frozen=True, slots=True)
class Selection:
    selected: ChunkingCandidate
    reason: str


def select_chunking(candidates: Sequence[ChunkingCandidate]) -> Selection:
    if not candidates:
        raise ValueError("there is no chunking to select")
    ordered = sorted(candidates, key=_chunking_key)
    selected = ordered[0]
    if len(ordered) == 1:
        return Selection(selected, f"`{selected.label}` es el único chunking evaluado.")
    return Selection(selected, _reason(selected, ordered[1]))


def pick_best(runs: Sequence[StrategyRun], preference: Sequence[str]) -> StrategyRun:
    """Highest MRR, then highest recall@3, then the earliest name in `preference`."""
    if not runs:
        raise ValueError("there is no strategy to pick")
    unknown = [run.strategy for run in runs if run.strategy not in preference]
    if unknown:
        raise ValueError(f"no declared preference for {unknown}")
    return min(
        runs,
        key=lambda run: (
            -round(run.mrr, _DIGITS),
            -round(run.recall_at_3, _DIGITS),
            preference.index(run.strategy),
        ),
    )


def _chunking_key(candidate: ChunkingCandidate) -> tuple[float, float, int, int, int]:
    return (
        -round(candidate.hybrid_mrr, _DIGITS),
        -round(candidate.hybrid_recall_at_3, _DIGITS),
        candidate.chunks,
        KIND_ORDER[candidate.kind],
        candidate.grid_position,
    )


def _reason(selected: ChunkingCandidate, runner_up: ChunkingCandidate) -> str:
    lead = f"`{selected.label}` frente a `{runner_up.label}` (el segundo)"
    if round(selected.hybrid_mrr, _DIGITS) != round(runner_up.hybrid_mrr, _DIGITS):
        return (
            f"{lead}: tiene el MRR híbrido más alto "
            f"({selected.hybrid_mrr:.3f} contra {runner_up.hybrid_mrr:.3f})."
        )
    tie = f"{lead}: empatan en MRR híbrido ({selected.hybrid_mrr:.3f})"
    if round(selected.hybrid_recall_at_3, _DIGITS) != round(runner_up.hybrid_recall_at_3, _DIGITS):
        return (
            f"{tie} y gana por recall@3 híbrido "
            f"({selected.hybrid_recall_at_3:.3f} contra {runner_up.hybrid_recall_at_3:.3f})."
        )
    tie = f"{tie} y en recall@3 híbrido ({selected.hybrid_recall_at_3:.3f})"
    if selected.chunks != runner_up.chunks:
        return f"{tie}; gana por tener menos chunks ({selected.chunks} contra {runner_up.chunks})."
    tie = f"{tie} y en cantidad de chunks ({selected.chunks})"
    if selected.kind != runner_up.kind:
        return (
            f"{tie}; gana por ser un tipo más simple ({selected.kind} antes que {runner_up.kind})."
        )
    return f"{tie}, y son del mismo tipo; gana por el orden de la grilla."
