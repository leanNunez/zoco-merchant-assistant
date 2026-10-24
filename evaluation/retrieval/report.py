"""The saved results: `metrics.json` for machines and `report.md` for people.

Both are pure functions of the evaluation and its provenance, with no wall-clock time, so
running the evaluation again over the same corpus and cache produces identical files.
The report is in Spanish, like the rest of the project's documentation.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from evaluation.retrieval.pipeline import (
    MAIN_STRATEGIES,
    META_PREFERENCE,
    SINGLE_PREFERENCE,
    AblationDelta,
    MainRow,
    RetrievalEvaluation,
)
from evaluation.retrieval.provenance import Provenance
from evaluation.retrieval.scoring import (
    BOOTSTRAP_RESAMPLES,
    BOOTSTRAP_SEED,
    Interval,
    QuestionOutcome,
    StrategyRun,
)
from evaluation.retrieval.selection import SELECTION_RULE
from merchant_assistant.knowledge.questions import Question

# Six decimals are far below what 20 questions can resolve (one question moves a mean by
# 0.05) and keep the JSON stable against last-bit float noise.
_DECIMALS = 6
_SHORT_QUESTION = 60


def to_json(
    evaluation: RetrievalEvaluation, provenance: Provenance, questions: Sequence[Question]
) -> str:
    main_runs = {row.run.strategy: row.run for row in evaluation.main}
    document: dict[str, object] = {
        "provenance": {
            "embeddings_model": provenance.embeddings_model,
            "reference_date": provenance.reference_date.isoformat(),
            "corpus_sha256": provenance.corpus_sha256,
            "chunk_counts": provenance.chunk_counts,
            "api_calls": provenance.api_calls,
            "bootstrap": {"resamples": BOOTSTRAP_RESAMPLES, "seed": BOOTSTRAP_SEED},
        },
        "selection_rule": SELECTION_RULE,
        "selected_chunking": {
            "label": evaluation.selection.selected.label,
            "reason": evaluation.selection.reason,
        },
        "grid": [_summary(run) for run in evaluation.grid],
        "main": [_main_row(row) for row in evaluation.main],
        "paired_differences": [
            {
                "label": comparison.label,
                "metric": "mrr",
                "better": comparison.better,
                "baseline": comparison.baseline,
                "mean_difference": _round(comparison.mean_difference),
                "ci95": _interval(comparison.interval),
            }
            for comparison in evaluation.paired
        ],
        "ablations": [_summary(run) for run in evaluation.ablations],
        "ablation_deltas": [_delta_json(delta) for delta in evaluation.ablation_deltas],
        "per_question": [
            {
                "id": question.id,
                "question": question.text,
                "relevant": sorted(question.relevant),
                "reference_period": {
                    "start": evaluation.periods[question.id].start.isoformat(),
                    "end": evaluation.periods[question.id].end.isoformat(),
                    "source": evaluation.periods[question.id].source,
                },
                "strategies": {
                    name: _outcome(_outcome_of(main_runs[name], question.id))
                    for name in MAIN_STRATEGIES
                },
            }
            for question in questions
        ],
    }
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def to_markdown(
    evaluation: RetrievalEvaluation, provenance: Provenance, questions: Sequence[Question]
) -> str:
    selected = evaluation.selection.selected.label
    lines = [
        "# Evaluación de retrieval",
        "",
        "Generado por `make eval-retrieval` (`uv run python -m evaluation.retrieval run`). "
        "No editar a mano: se regenera igual en cada corrida.",
        "",
        "## Procedencia",
        "",
        f"- Modelo de embeddings: `{provenance.embeddings_model}`",
        f'- Fecha de referencia ("hoy"): {provenance.reference_date.isoformat()}, fija para '
        "que el resultado no dependa del día en que se corre",
        f"- sha256 del corpus (`docs/*.md` y `questions.jsonl`): `{provenance.corpus_sha256}`",
        f"- Llamadas a la API de embeddings durante esta corrida: {provenance.api_calls}",
        "- Chunks por estrategia de chunking: "
        + ", ".join(f"`{label}` {count}" for label, count in provenance.chunk_counts.items()),
        "",
        "## Reglas declaradas antes de ver las tablas",
        "",
        SELECTION_RULE,
        "",
        f"Orden fijo para el modo de metadata: {_names(META_PREFERENCE)}. Para la mejor "
        f"estrategia simple: {_names(SINGLE_PREFERENCE)}.",
        "",
        "## Definiciones",
        "",
        "- La unidad del ranking es el documento: cada documento cuenta una vez, en la "
        "posición de su mejor chunk. A cada estrategia se le piden todos los chunks, así el "
        "MRR ve la lista completa.",
        "- recall@3 = documentos relevantes en el top 3 / documentos relevantes. Con dos "
        "relevantes y uno encontrado vale 0.5.",
        "- hit@3 = 1 si algún documento relevante está en el top 3.",
        "- MRR = promedio de 1 / posición del primer documento relevante (0 si no aparece).",
        f"- Intervalos: bootstrap percentil al 95 %, {BOOTSTRAP_RESAMPLES} remuestreos de "
        f"las preguntas con `numpy.random.default_rng({BOOTSTRAP_SEED})`; todas las "
        "estrategias se remuestrean con los mismos sorteos.",
        f"- Hay {len(questions)} preguntas: **una sola pregunta mueve un promedio "
        f"{100 / len(questions):g} puntos** ({1 / len(questions):.2f} en recall@3, hit@3 o "
        "MRR). Una diferencia de ese tamaño no alcanza para preferir una estrategia.",
        "",
        "## Grilla de chunking",
        "",
        "BM25 sin stemming. Híbrido = RRF (k = 60) sobre los rankings de chunks de vector y BM25.",
        "",
        "| Chunking | Chunks | Estrategia | recall@3 | hit@3 | MRR |",
        "|---|---:|---|---:|---:|---:|",
        *(
            f"| `{run.chunking}` | {run.chunks} | {run.strategy} | {run.recall_at_3:.3f} "
            f"| {run.hit_at_3:.3f} | {run.mrr:.3f} |"
            for run in evaluation.grid
        ),
        "",
        "## Chunking seleccionado",
        "",
        f"**`{selected}`**. {evaluation.selection.reason}",
        "",
        f"## Tabla principal (`{selected}`)",
        "",
        "| Estrategia | recall@3 | IC 95 % | hit@3 | MRR | IC 95 % |",
        "|---|---:|---|---:|---:|---|",
        *(_main_line(row) for row in evaluation.main),
        "",
        "## Diferencias pareadas de MRR",
        "",
        "Diferencia por pregunta y bootstrap de su promedio. Un intervalo que contiene el 0 "
        "no muestra una diferencia.",
        "",
        "| Comparación | Diferencia de MRR | IC 95 % |",
        "|---|---:|---|",
        *(
            f"| {c.label}: {c.better} - {c.baseline} | {c.mean_difference:+.3f} "
            f"| {_interval_text(c.interval)} |"
            for c in evaluation.paired
        ),
        "",
        "## Ablaciones",
        "",
        "Informativas: **nunca cambian la selección declarada** (el chunking y los modos de la "
        "tabla principal se eligieron antes de mirarlas).",
        "",
        f"- `bm25(stem)`: BM25 con el stemming de plurales, sobre `{selected}`.",
        "- La misma tabla con el encabezado (título, tipo y vigencia) cambiado: "
        f"`{_toggled_label(selected)}`.",
        "- `hybrid+kind-demotion(historico)`: baja los documentos de tipo `historico` sin "
        "mirar fechas, para ver si el tipo aporta algo que la vigencia no aporte.",
        "- `bm25(no parts)`: BM25 que emite `zt-pro-2` sin sus partes (`zt`, `pro`, `2`).",
        "- `bm25(stopwords)` y `bm25(stopwords, stem)`: BM25 sin una lista conservadora de "
        "palabras funcionales del español (artículos, preposiciones, conjunciones, pronombres, "
        "formas de ser, estar, haber e ir, interrogativos), sin y con stemming. Nunca quita "
        "`no`, `sin`, `mas`, `solo`, números ni palabras que puedan cambiar una política.",
        "- `hybrid(stopwords)` y `hybrid+meta(demote)(stopwords)`: el mismo híbrido con el BM25 "
        "sin palabras funcionales (el lado vectorial no cambia).",
        "- `hybrid(fuse by document)` y `hybrid+meta(demote)(fuse by document)`: RRF sobre "
        "documentos (cada uno en la posición de su mejor chunk) en lugar de sobre chunks. Con el "
        "chunking `document` hay un chunk por documento y la fusión no puede cambiar nada por "
        "construcción; por eso, si el chunking elegido no es `section`, se corre también sobre "
        "`section`.",
        "",
        "| Chunking | Estrategia | recall@3 | hit@3 | MRR |",
        "|---|---|---:|---:|---:|",
        *(
            f"| `{run.chunking}` | {run.strategy} | {run.recall_at_3:.3f} | {run.hit_at_3:.3f} "
            f"| {run.mrr:.3f} |"
            for run in evaluation.ablations
        ),
        "",
        "### Qué se movió",
        "",
        "Cada variante contra la estrategia que modifica. Una pregunta **mejora** si su par "
        "(rango recíproco, recall@3) sube y **empeora** si baja.",
        "",
        "| Variante | Contra | Δ recall@3 | Δ hit@3 | Δ MRR | Mejoran | Empeoran |",
        "|---|---|---:|---:|---:|---|---|",
        *(_delta_line(delta) for delta in evaluation.ablation_deltas),
        "",
        *_movement_reading(evaluation.ablation_deltas, len(questions)),
        "",
        "## Por pregunta",
        "",
        f"Sobre `{selected}`. Cada celda: posición del primer documento relevante (— si no "
        "aparece) y el top 3 de documentos. **✗** marca las preguntas en las que alguna "
        "estrategia deja afuera del top 3 algún documento relevante; la última columna dice "
        "cuáles.",
        "",
        "| | Pregunta | Relevantes | Período | "
        + " | ".join(MAIN_STRATEGIES)
        + " | Fallan en top 3 |",
        "|---|---|---|---|" + "---|" * len(MAIN_STRATEGIES) + "---|",
        *_per_question_lines(evaluation, questions),
        "",
    ]
    return "\n".join(lines)


def _per_question_lines(
    evaluation: RetrievalEvaluation, questions: Sequence[Question]
) -> list[str]:
    runs = {row.run.strategy: row.run for row in evaluation.main}
    lines: list[str] = []
    for question in questions:
        outcomes = {name: _outcome_of(runs[name], question.id) for name in MAIN_STRATEGIES}
        cells = [_cell(outcomes[name]) for name in MAIN_STRATEGIES]
        misses = [
            name + ("" if outcomes[name].hit_at_3 == 0 else " (parcial)")
            for name in MAIN_STRATEGIES
            if outcomes[name].recall_at_3 < 1
        ]
        period = evaluation.periods[question.id]
        period_text = (
            "hoy"
            if period.source == "today"
            else f"{period.start.isoformat()} a {period.end.isoformat()}"
        )
        lines.append(
            f"| {question.id}{' ✗' if misses else ''} | {_short(question.text)} "
            f"| {', '.join(sorted(question.relevant))} | {period_text} | "
            + " | ".join(cells)
            + f" | {', '.join(misses) if misses else '—'} |"
        )
    return lines


def _label(chunking: str, strategy: str) -> str:
    return f"`{chunking}` {strategy}"


def _delta_line(delta: AblationDelta) -> str:
    return (
        f"| {_label(delta.chunking, delta.strategy)} "
        f"| {_label(delta.reference_chunking, delta.reference)} "
        f"| {delta.recall_delta:+.3f} | {delta.hit_delta:+.3f} | {delta.mrr_delta:+.3f} "
        f"| {', '.join(delta.improved) or '—'} | {', '.join(delta.worsened) or '—'} |"
    )


def _movement_reading(deltas: Sequence[AblationDelta], total: int) -> list[str]:
    """In plain words: which variants moved no question, one, or several."""
    still = [d for d in deltas if d.moved == 0]
    one = [d for d in deltas if d.moved == 1]
    several = [d for d in deltas if d.moved >= 2]

    def names(group: Sequence[AblationDelta]) -> str:
        return ", ".join(_label(d.chunking, d.strategy) for d in group) or "ninguna"

    return [
        f"- No mueven ninguna de las {total} preguntas: {names(still)}.",
        f"- Mueven exactamente una pregunta: {names(one)}. Una pregunta es "
        f"{100 / total:g} puntos de cualquier promedio: **una diferencia de una pregunta no es "
        "evidencia**, es lo que cambia un promedio por azar de redacción.",
        f"- Mueven dos o más preguntas: {names(several)}. Con {total} preguntas y los "
        "intervalos de arriba, ni siquiera eso alcanza para cambiar lo ya elegido.",
    ]


def _delta_json(delta: AblationDelta) -> dict[str, object]:
    return {
        "chunking": delta.chunking,
        "strategy": delta.strategy,
        "reference_chunking": delta.reference_chunking,
        "reference": delta.reference,
        "recall_at_3_delta": _round(delta.recall_delta),
        "hit_at_3_delta": _round(delta.hit_delta),
        "mrr_delta": _round(delta.mrr_delta),
        "improved": list(delta.improved),
        "worsened": list(delta.worsened),
    }


def _toggled_label(selected: str) -> str:
    return (
        selected.removesuffix("+header") if selected.endswith("+header") else f"{selected}+header"
    )


def _main_line(row: MainRow) -> str:
    run = row.run
    return (
        f"| {run.strategy} | {run.recall_at_3:.3f} | {_interval_text(row.recall_interval)} "
        f"| {run.hit_at_3:.3f} | {run.mrr:.3f} | {_interval_text(row.mrr_interval)} |"
    )


def _cell(outcome: QuestionOutcome) -> str:
    rank = "—" if outcome.first_relevant_rank is None else str(outcome.first_relevant_rank)
    top = ", ".join(outcome.ranking[:3]) or "(vacío)"
    return f"{rank} · {top}"


def _summary(run: StrategyRun) -> dict[str, object]:
    return {
        "chunking": run.chunking,
        "chunks": run.chunks,
        "strategy": run.strategy,
        "recall_at_3": _round(run.recall_at_3),
        "hit_at_3": _round(run.hit_at_3),
        "mrr": _round(run.mrr),
    }


def _main_row(row: MainRow) -> dict[str, object]:
    return {
        **_summary(row.run),
        "recall_at_3_ci95": _interval(row.recall_interval),
        "mrr_ci95": _interval(row.mrr_interval),
    }


def _outcome(outcome: QuestionOutcome) -> dict[str, object]:
    return {
        "first_relevant_rank": outcome.first_relevant_rank,
        "top3": list(outcome.ranking[:3]),
        "recall_at_3": _round(outcome.recall_at_3),
        "hit_at_3": _round(outcome.hit_at_3),
        "reciprocal_rank": _round(outcome.reciprocal_rank),
    }


def _outcome_of(run: StrategyRun, question_id: str) -> QuestionOutcome:
    return next(outcome for outcome in run.outcomes if outcome.question_id == question_id)


def _interval(interval: Interval) -> list[float]:
    return [_round(interval.low), _round(interval.high)]


def _interval_text(interval: Interval) -> str:
    return f"[{interval.low:.3f}, {interval.high:.3f}]"


def _round(value: float) -> float:
    # `+ 0.0` turns a negative zero into a plain zero, so it never prints as "-0.0".
    return round(value, _DECIMALS) + 0.0


def _short(text: str) -> str:
    clean = text.replace("|", "/")
    return clean if len(clean) <= _SHORT_QUESTION else clean[: _SHORT_QUESTION - 1].rstrip() + "…"


def _names(names: Sequence[str]) -> str:
    return " > ".join(f"`{name}`" for name in names)
