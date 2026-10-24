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
from evaluation.retrieval.tiebreak import RRF_K, Metrics, TieBreakRow
from merchant_assistant.knowledge.questions import Question

# Six decimals are far below what 20 questions can resolve (one question moves a mean by
# 0.05) and keep the JSON stable against last-bit float noise.
_DECIMALS = 6
_SHORT_QUESTION = 60
TIE_BREAK_METHOD = "exact enumeration of the permutations inside each exact-tie group; no sampling"
# Means of the same values in another order can differ in the last bit.
_SAME = 1e-9


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
        "tie_break": {
            "method": TIE_BREAK_METHOD,
            "rrf_k": RRF_K,
            "strategies": [_tie_break_json(row) for row in evaluation.tie_break],
        },
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
        *_tie_break_section(evaluation, selected),
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


def _tie_break_section(evaluation: RetrievalEvaluation, selected: str) -> list[str]:
    rows = evaluation.tie_break
    if not rows:
        return []
    variants = (
        ("por defecto", "default"),
        ("invertido", "reversed"),
        ("esperado", "expected"),
    )
    table = [
        "| Estrategia | Desempate | recall@3 | hit@3 | MRR |",
        "|---|---|---:|---:|---:|",
    ]
    for row in rows:
        for label, attribute in variants:
            metrics: Metrics = getattr(row, attribute)
            table.append(
                f"| {row.strategy} | {label} | {metrics.recall_at_3:.3f} "
                f"| {metrics.hit_at_3:.3f} | {metrics.mrr:.3f} |"
            )
        table.append(f"| {row.strategy} | peor orden posible | — | — | {row.worst_mrr:.3f} |")
        table.append(f"| {row.strategy} | mejor orden posible | — | — | {row.best_mrr:.3f} |")
    return [
        "## Sensibilidad al desempate de RRF",
        "",
        "Informativa: **no cambia la selección declarada**. En RRF dos documentos pueden sumar "
        "exactamente el mismo puntaje: si A es primero en vector y segundo en BM25, y B al "
        f"revés, los dos suman 1/{RRF_K + 1} + 1/{RRF_K + 2}. `knowledge/fusion.py` desempata "
        "por mejor rango y luego por id ascendente: una convención arbitraria, no evidencia. "
        f"Acá se recalcula la fusión sobre `{selected}` con un RRF propio de "
        "`evaluation/retrieval/tiebreak.py` (el de producción no se toca):",
        "",
        "- **por defecto**: la convención de producción; se verifica que reproduce las filas "
        "de la tabla principal.",
        "- **invertido**: mejor rango y luego id descendente.",
        "- **esperado**: promedio exacto sobre todos los órdenes posibles de cada grupo de "
        "empate exacto, ordenado al azar con igual probabilidad. Se calcula enumerando las "
        "permutaciones de cada grupo (son de dos documentos): no hay sorteo y el resultado es "
        "idéntico en cada corrida.",
        "- **peor / mejor orden posible**: el MRR si en cada pregunta el empate cayera siempre "
        "en el peor (o el mejor) orden.",
        "",
        *table,
        "",
        *_tie_break_lists(rows, len(evaluation.periods)),
        "",
        "Lectura:",
        "",
        *_tie_break_reading(evaluation),
        "",
    ]


def _tie_break_lists(rows: Sequence[TieBreakRow], total: int) -> list[str]:
    def ids(found: Sequence[str]) -> str:
        return ", ".join(found) or "ninguna"

    lines = [
        f"- Preguntas con un empate exacto entre dos documentos ({len(rows[0].with_ties)} de "
        f"{total}): {ids(rows[0].with_ties)}."
    ]
    lines.extend(
        f"- `{row.strategy}`: el orden de los dos primeros documentos depende del desempate "
        f"en {ids(row.first_two_depend)}; el rango recíproco o el recall@3 cambian en "
        f"{ids(row.outcome_depends)}."
        for row in rows
    )
    return lines


def _tie_break_reading(evaluation: RetrievalEvaluation) -> list[str]:
    """In plain words: how much of the hybrid advantage is tie-break luck, and the policy."""
    rows = {row.strategy: row for row in evaluation.tie_break}
    runs = {row.run.strategy: row.run for row in evaluation.main}
    if not {"hybrid", "hybrid+meta(demote)"} <= rows.keys() or "bm25" not in runs:
        return []
    hybrid, policy, bm25 = rows["hybrid"], rows["hybrid+meta(demote)"], runs["bm25"].mrr
    values = (hybrid.default.mrr, hybrid.reversed.mrr, hybrid.expected.mrr)
    low, high = min(values), max(values)
    lines = []
    if high - low < _SAME:
        lines.append(f"- El MRR del híbrido ({hybrid.default.mrr:.3f}) no depende del desempate.")
    else:
        verdict = (
            "En el peor orden posible el híbrido queda por debajo de BM25."
            if hybrid.worst_mrr < bm25 - _SAME
            else "Aun en el peor orden posible el híbrido queda por encima de BM25."
        )
        lines.append(
            "- **La ventaja del híbrido sobre BM25 es en parte suerte del desempate.** Con la "
            f"convención de producción el MRR del híbrido es {hybrid.default.mrr:.3f} contra "
            f"{bm25:.3f} de BM25 ({hybrid.default.mrr - bm25:+.3f}); con el desempate invertido "
            f"es {hybrid.reversed.mrr:.3f} ({hybrid.reversed.mrr - bm25:+.3f}) y el valor "
            f"esperado es {hybrid.expected.mrr:.3f} ({hybrid.expected.mrr - bm25:+.3f}). El "
            f"rango de las tres variantes es [{low:.3f}, {high:.3f}] y el de todos los órdenes "
            f"posibles, [{hybrid.worst_mrr:.3f}, {hybrid.best_mrr:.3f}]. {verdict}"
        )
    gains = [
        policy.default.mrr - hybrid.default.mrr,
        policy.reversed.mrr - hybrid.reversed.mrr,
        policy.expected.mrr - hybrid.expected.mrr,
    ]
    lines.append(
        f"- Efecto de la política de vigencia sobre el MRR del híbrido: {gains[0]:+.3f} con el "
        f"desempate de producción, {gains[1]:+.3f} invertido y {gains[2]:+.3f} esperado."
    )
    resolved = [q for q in hybrid.outcome_depends if q not in policy.outcome_depends]
    remaining = policy.outcome_depends
    if resolved:
        lines.append(
            f"- {', '.join(resolved)} dependían del desempate en el híbrido y con "
            "`hybrid+meta(demote)` ya no: la política ordena por vigencia antes de mirar el "
            "empate, así que su efecto en esas preguntas es una propiedad de la política y no "
            "del desempate."
        )
    if remaining:
        lines.append(
            f"- `hybrid+meta(demote)` sigue dependiendo del desempate en {', '.join(remaining)}: "
            f"su MRR va de {policy.worst_mrr:.3f} a {policy.best_mrr:.3f} según el orden "
            f"(esperado {policy.expected.mrr:.3f}), porque la política no resuelve ese "
            "empate."
        )
    else:
        lines.append(
            f"- El MRR de `hybrid+meta(demote)` ({policy.default.mrr:.3f}) no depende del "
            "desempate."
        )
    lines.append(
        "- Con estas diferencias de una o dos preguntas no se puede declarar un ganador entre "
        "vector, BM25 e híbrido."
    )
    return lines


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


def _tie_break_json(row: TieBreakRow) -> dict[str, object]:
    return {
        "strategy": row.strategy,
        "chunking": row.chunking,
        "default": _metrics_json(row.default),
        "reversed": _metrics_json(row.reversed),
        "expected": _metrics_json(row.expected),
        "worst_case_mrr": _round(row.worst_mrr),
        "best_case_mrr": _round(row.best_mrr),
        "questions_with_exact_tie": list(row.with_ties),
        "first_two_order_depends": list(row.first_two_depend),
        "outcome_depends": list(row.outcome_depends),
    }


def _metrics_json(metrics: Metrics) -> dict[str, float]:
    return {
        "recall_at_3": _round(metrics.recall_at_3),
        "hit_at_3": _round(metrics.hit_at_3),
        "mrr": _round(metrics.mrr),
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
