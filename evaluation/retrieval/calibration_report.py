"""`gate_calibration.md` (for people) and `gate_calibration.json` (for the drift test).

Pure functions of the calibration and its provenance, with no wall-clock time: running the
calibration again over the same corpus, queries and cache produces identical files. Spanish,
like the rest of the project's documentation.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass

from evaluation.retrieval.calibration import (
    GATE_RULES,
    LIMITS,
    PREVIOUS_BM25_FLOOR,
    TOKENIZERS,
    Calibration,
    Confusion,
    GateChoice,
    Group,
    QueryScores,
    Spread,
    TokenizerName,
    TokenizerResult,
    answers,
    spread,
)

_DECIMALS = 6
_SHORT = 52
_TOKENIZER_LABEL: dict[TokenizerName, str] = {
    "default": "tokenizador por defecto",
    "stopwords": "tokenizador con stopwords",
}
_GROUP_LABEL: dict[Group, str] = {
    "kit": "kit (preguntas de `questions.jsonl`)",
    "short_legit": "short_legit (frases cortas legítimas)",
    "off_topic": "off_topic (fuera del dominio)",
}


@dataclass(frozen=True, slots=True)
class CalibrationProvenance:
    embeddings_model: str
    chunking: str
    chunks: int
    corpus_sha256: str
    api_calls: int


def to_json(calibration: Calibration, provenance: CalibrationProvenance) -> str:
    chosen = calibration.chosen
    document: dict[str, object] = {
        "provenance": {
            "embeddings_model": provenance.embeddings_model,
            "chunking": provenance.chunking,
            "chunks": provenance.chunks,
            "corpus_sha256": provenance.corpus_sha256,
            "api_calls": provenance.api_calls,
            "queries": {group: _count(calibration, group) for group in _GROUP_LABEL},
        },
        "rules": list(GATE_RULES),
        "selected_tokenizer": calibration.selected,
        "stopwords": {
            "adopted": calibration.decision.adopted,
            "recall_not_lower": calibration.decision.recall_not_lower,
            "mrr_not_lower": calibration.decision.mrr_not_lower,
            "margin_improves": calibration.decision.margin_improves,
        },
        "thresholds": {
            "bm25_degraded": _round(chosen.degraded.bm25.value),
            "bm25_hybrid": _round(chosen.hybrid.bm25.value),
            "cosine_hybrid": _round(_cosine(chosen.hybrid)),
        },
        "tokenizers": {
            name: _tokenizer_json(result) for name, result in calibration.results.items()
        },
        "previous_floor": {
            "bm25": PREVIOUS_BM25_FLOOR,
            "legitimate_rejected": list(calibration.previous_floor_misses),
            "off_topic_accepted": calibration.previous_floor_off_accepted,
        },
    }
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def to_markdown(calibration: Calibration, provenance: CalibrationProvenance) -> str:
    chosen = calibration.chosen
    scored = calibration.scored
    lines = [
        "# Calibración de la compuerta `no_data`",
        "",
        "Generado por `make eval-retrieval` (`uv run python -m evaluation.retrieval calibrate`). "
        "No editar a mano: se regenera igual en cada corrida.",
        "",
        "La compuerta decide cuándo `buscar_documentacion` dice que la documentación no tiene nada "
        "sobre la consulta. Un `no_data` falso es seguro pero inútil (la conversación pasa a "
        "SUGGEST y el cliente no recibe nada hasta que un operador aprueba); un `ok` falso solo "
        "le da al modelo pasajes poco relacionados, y el modelo tiene la instrucción de no "
        "inventar. Por eso la regla prefiere no disparar de más.",
        "",
        "## Procedencia",
        "",
        f"- Modelo de embeddings: `{provenance.embeddings_model}`",
        f"- Chunking: `{provenance.chunking}` ({provenance.chunks} chunks), el que seleccionó la "
        "regla declarada de `report.md`",
        f"- sha256 del corpus, `questions.jsonl` y `calibration_queries.jsonl`: "
        f"`{provenance.corpus_sha256}`",
        f"- Consultas: {_count(calibration, 'kit')} del kit, {_count(calibration, 'short_legit')} "
        f"cortas legítimas y {_count(calibration, 'off_topic')} fuera de tema, "
        f"{sum(s.query.hard for s in scored)} de ellas `hard`",
        f"- Llamadas a la API de embeddings durante esta corrida: {provenance.api_calls}",
        "",
        "## Reglas declaradas antes de medir",
        "",
        *GATE_RULES,
        "",
        "## Los puntajes por grupo",
        "",
        "Mejor puntaje de cada consulta sobre los chunks. Cada celda: mínimo / mediana / máximo.",
        "",
        "| Grupo | Consultas | BM25 (por defecto) | BM25 (stopwords) | Coseno |",
        "|---|---:|---|---|---|",
        *_spread_rows(scored),
        "",
        "### Solapamiento",
        "",
        "Distancia entre la consulta legítima (kit o short_legit) más débil y la fuera de tema más "
        "fuerte; negativa si se solapan. El margen relativo es esa distancia sobre el puntaje "
        "legítimo más débil.",
        "",
        "| Señal | Legítima más débil | Fuera de tema más fuerte | Margen | Margen relativo | "
        "Fuera de tema que la igualan o superan | Legítimas por debajo de la más fuerte |",
        "|---|---|---|---:|---:|---:|---:|",
        *_overlap_rows(calibration),
        "",
        "## Umbrales elegidos",
        "",
        "Cada umbral se da con el intervalo de umbrales equivalentes del que sale (cualquier valor "
        "dentro rechaza exactamente las mismas consultas medidas).",
        "",
        "| Tokenizador | Compuerta | τ_b (BM25) | τ_v (coseno) | Fuera de tema rechazadas "
        "| Celdas empatadas | Relajada |",
        "|---|---|---|---|---:|---:|---|",
        *_threshold_rows(calibration),
        "",
        f"**Seleccionado para el agente: {_TOKENIZER_LABEL[calibration.selected]}.** "
        f"Compuerta degradada: `no_data` si BM25 < {chosen.degraded.bm25.value:g}. Compuerta "
        f"híbrida: `no_data` si BM25 < {chosen.hybrid.bm25.value:g} y coseno < "
        f"{_cosine(chosen.hybrid):g}.",
        "",
        *_alternatives(calibration),
        "## Matrices de confusión",
        "",
        "Cuántas consultas de cada grupo responde la compuerta (no dice `no_data`) y cuántas "
        "rechaza. Lo bueno es responder todas las legítimas y rechazar las fuera de tema.",
        "",
        *_confusion_section(calibration),
        "## Qué consultas quedan mal",
        "",
        *_misclassified(calibration),
        "## El piso anterior",
        "",
        f"El piso anterior (BM25 ≥ {PREVIOUS_BM25_FLOOR:g} sobre chunking `section`, calibrado con "
        "las 20 preguntas del kit y 16 fuera de tema) sobre estas mismas consultas:",
        "",
        f"- Consultas legítimas que rechaza: {len(calibration.previous_floor_misses)} de "
        f"{_count(calibration, 'kit') + _count(calibration, 'short_legit')} "
        f"({_names(calibration, calibration.previous_floor_misses)}).",
        f"- Consultas fuera de tema que deja pasar: {calibration.previous_floor_off_accepted} de "
        f"{_count(calibration, 'off_topic')}.",
        "",
        "## Tokenizador con stopwords: decisión",
        "",
        *_stopword_section(calibration),
        "## Por consulta",
        "",
        f"Con el {_TOKENIZER_LABEL[calibration.selected]}. **Responde** / **Rechaza** es lo que "
        "hace cada compuerta; ✗ marca una decisión equivocada (legítima rechazada, fuera de tema "
        "que pasa).",
        "",
        "| Id | Grupo | Consulta | BM25 | BM25 (stopwords) | Coseno | Doc BM25 | Doc coseno "
        "| Degradada | Híbrida |",
        "|---|---|---|---:|---:|---:|---|---|---|---|",
        *(_query_row(s, calibration) for s in scored),
        "",
        "## Límites",
        "",
        *(f"- {limit}" for limit in LIMITS),
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- sections


def _spread_rows(scored: Sequence[QueryScores]) -> list[str]:
    off = [s for s in scored if s.query.group == "off_topic"]
    groups: list[tuple[str, list[QueryScores]]] = [
        (_GROUP_LABEL["kit"], [s for s in scored if s.query.group == "kit"]),
        (_GROUP_LABEL["short_legit"], [s for s in scored if s.query.group == "short_legit"]),
        (_GROUP_LABEL["off_topic"], off),
        ("  de ellas `hard`", [s for s in off if s.query.hard]),
        ("  de ellas las demás", [s for s in off if not s.query.hard]),
    ]
    rows = []
    for label, members in groups:
        rows.append(
            f"| {label} | {len(members)} "
            f"| {_spread([s.bm25 for s in members])} "
            f"| {_spread([s.bm25_stopwords for s in members])} "
            f"| {_spread([s.cosine for s in members])} |"
        )
    return rows


def _overlap_rows(calibration: Calibration) -> list[str]:
    scored = calibration.scored
    legit = [s for s in scored if s.query.group in ("kit", "short_legit")]
    off = [s for s in scored if s.query.group == "off_topic"]
    rows = []
    signals: list[tuple[str, list[float], list[float]]] = [
        ("BM25 (por defecto)", [s.bm25 for s in legit], [s.bm25 for s in off]),
        ("BM25 (stopwords)", [s.bm25_stopwords for s in legit], [s.bm25_stopwords for s in off]),
        ("Coseno", [s.cosine for s in legit], [s.cosine for s in off]),
    ]
    for name, legit_values, off_values in signals:
        weakest, strongest = min(legit_values), max(off_values)
        margin = weakest - strongest
        relative = margin / weakest if weakest else None
        weakest_id = legit[legit_values.index(weakest)].query.id
        strongest_id = off[off_values.index(strongest)].query.id
        rows.append(
            f"| {name} | {weakest:.3f} ({weakest_id}) | {strongest:.3f} ({strongest_id}) "
            f"| {margin:+.3f} | {'indefinido' if relative is None else f'{relative:+.3f}'} "
            f"| {sum(value >= weakest for value in off_values)} "
            f"| {sum(value <= strongest for value in legit_values)} |"
        )
    return rows


def _threshold_rows(calibration: Calibration) -> list[str]:
    rows = []
    for name in TOKENIZERS:
        result = calibration.results[name]
        mark = " (seleccionado)" if name == calibration.selected else ""
        for label, choice in (("degradada", result.degraded), ("híbrida", result.hybrid)):
            cosine = "—" if choice.cosine is None else f"{choice.cosine.value:g}"
            rows.append(
                f"| {_TOKENIZER_LABEL[name]}{mark} | {label} "
                f"| {choice.bm25.value:g} ({choice.bm25.low:.3f}, {choice.bm25.high:.3f}] "
                f"| {cosine}{_interval(choice)} | {choice.rejected_off_topic} "
                f"| {choice.tied_cells} | {'sí' if choice.relaxed else 'no'} |"
            )
    return rows


def _interval(choice: GateChoice) -> str:
    axis = choice.cosine
    return "" if axis is None else f" ({axis.low:.4f}, {axis.high:.4f}]"


def _alternatives(calibration: Calibration) -> list[str]:
    """What the chosen gates amount to, in plain words, and the alternative that was not used."""
    chosen = calibration.chosen
    scored = calibration.scored
    name = calibration.selected
    hybrid_cosine = _cosine(chosen.hybrid)
    off = [s for s in scored if s.query.group == "off_topic"]
    legit = [s for s in scored if s.query.group != "off_topic"]
    cosine_off = sum(s.cosine < hybrid_cosine for s in off)
    cosine_legit = sum(s.cosine < hybrid_cosine for s in legit)
    rescued = [
        s
        for s in scored
        if s.cosine < hybrid_cosine and s.bm25_of(name) >= chosen.hybrid.bm25.value
    ]
    kit_only = calibration.chosen.kit_only_degraded
    by_id = {s.query.id: s for s in scored}
    lost = [by_id[i] for i in kit_only.missed]
    return [
        "### Qué significan estos umbrales",
        "",
        f"- **Híbrida.** Una compuerta de solo coseno con τ_v = {hybrid_cosine:g} rechazaría "
        f"{cosine_off} de {len(off)} consultas fuera de tema y {cosine_legit} de {len(legit)} "
        "legítimas; la compuerta híbrida rechaza "
        f"{chosen.hybrid.rejected_off_topic} fuera de tema. BM25 solo rescata una consulta de "
        f"coseno bajo si pasa de {chosen.hybrid.bm25.value:g}: en estas consultas rescata "
        f"{len(rescued)}. Dicho claro: con estos datos el coseno hace el trabajo y el umbral de "
        "BM25 queda en el medio de una meseta ancha de valores equivalentes.",
        f"- **Degradada.** El umbral es {chosen.degraded.bm25.value:g}: por debajo solo quedan "
        "las consultas que no comparten ninguna palabra con el corpus (por ejemplo `hola`). BM25 "
        "no tiene stopwords, así que casi cualquier frase en español comparte `el`, `de` o `la` "
        "con los documentos y puntúa por encima. La regla pide que ninguna consulta legítima "
        "quede rechazada, y las legítimas cortas puntúan casi cero: la compuerta degradada es, "
        "por la regla, casi vacía, y el modo sin claves deja pasar casi todo (falla tolerable).",
        f"- **Alternativa que no se usa: exigir solo el kit en modo degradado.** El umbral sería "
        f"{kit_only.bm25.value:g}, rechazaría {kit_only.rejected_off_topic} de {len(off)} fuera de "
        f"tema y dejaría sin respuesta {len(lost)} consultas legítimas: "
        f"{_listing(lost) if lost else 'ninguna'}.",
        "",
    ]


def _confusion_section(calibration: Calibration) -> list[str]:
    lines: list[str] = []
    order = [calibration.selected, *(n for n in TOKENIZERS if n != calibration.selected)]
    for name in order:
        result = calibration.results[name]
        mark = " (seleccionado)" if name == calibration.selected else " (no seleccionado)"
        lines += [
            f"### {_TOKENIZER_LABEL[name].capitalize()}{mark}",
            "",
            "| Grupo | Degradada: responde | Degradada: rechaza | Híbrida: responde "
            "| Híbrida: rechaza |",
            "|---|---:|---:|---:|---:|",
            *_confusion_rows(result.degraded_confusion, result.hybrid_confusion),
            "",
        ]
    return lines


def _confusion_rows(degraded: Confusion, hybrid: Confusion) -> list[str]:
    def row(label: str, kept_d: int, total: int, kept_h: int) -> str:
        return f"| {label} ({total}) | {kept_d} | {total - kept_d} | {kept_h} | {total - kept_h} |"

    off_d = degraded.off_total - degraded.off_rejected
    off_h = hybrid.off_total - hybrid.off_rejected
    hard_d = degraded.hard_total - degraded.hard_rejected
    hard_h = hybrid.hard_total - hybrid.hard_rejected
    return [
        row("kit", degraded.kit_answered, degraded.kit_total, hybrid.kit_answered),
        row("short_legit", degraded.short_answered, degraded.short_total, hybrid.short_answered),
        row("off_topic", off_d, degraded.off_total, off_h),
        row("  de ellas `hard`", hard_d, degraded.hard_total, hard_h),
    ]


def _misclassified(calibration: Calibration) -> list[str]:
    lines: list[str] = []
    name = calibration.selected
    result = calibration.chosen
    for label, choice in (("degradada", result.degraded), ("híbrida", result.hybrid)):
        rejected = [
            s
            for s in calibration.scored
            if s.query.group != "off_topic" and not answers(s, choice, name)
        ]
        passed = [
            s
            for s in calibration.scored
            if s.query.group == "off_topic" and answers(s, choice, name)
        ]
        relaxed = " Se relajó la regla (a) para conservar el kit." if choice.relaxed else ""
        lines += [
            f"### Compuerta {label}",
            "",
            f"- Legítimas rechazadas: {_listing(rejected) if rejected else 'ninguna'}.{relaxed}",
            f"- Fuera de tema que pasan ({len(passed)} de "
            f"{sum(s.query.group == 'off_topic' for s in calibration.scored)}): "
            f"{_listing(passed) if passed else 'ninguna'}.",
            "",
        ]
    return lines


def _stopword_section(calibration: Calibration) -> list[str]:
    decision = calibration.decision
    default, stop = calibration.results["default"], calibration.results["stopwords"]
    verdict = (
        "se adopta" if decision.adopted else "no se adopta: se mantiene el tokenizador por defecto"
    )
    default_kit, stop_kit = decision.default_kit, decision.stopwords_kit
    return [
        "| Condición | Por defecto | Con stopwords | Se cumple |",
        "|---|---:|---:|---|",
        f"| (i) recall@3 de `hybrid+meta(demote)` sobre el kit "
        f"| {default_kit.recall_at_3:.3f} | {stop_kit.recall_at_3:.3f} "
        f"| {_yes(decision.recall_not_lower)} (no baja) |",
        f"| (i) MRR de `hybrid+meta(demote)` sobre el kit "
        f"| {default_kit.mrr:.3f} | {stop_kit.mrr:.3f} "
        f"| {_yes(decision.mrr_not_lower)} (no baja) |",
        f"| (ii) margen relativo de BM25 | {_relative(default)} "
        f"| {_relative(stop)} | {_yes(decision.margin_improves)} (sube) |",
        f"| margen absoluto de BM25 (informativo) | {default.margin:+.3f} "
        f"| {stop.margin:+.3f} | — |",
        "",
        f"Resultado: **{verdict}**.",
        "",
    ]


def _query_row(scores: QueryScores, calibration: Calibration) -> str:
    name = calibration.selected
    result = calibration.chosen
    query = scores.query
    group = "off_topic (hard)" if query.hard else query.group

    def verdict(choice: GateChoice) -> str:
        kept = answers(scores, choice, name)
        wrong = kept if query.group == "off_topic" else not kept
        return f"{'Responde' if kept else 'Rechaza'}{' ✗' if wrong else ''}"

    return (
        f"| {query.id} | {group} | {_short(query.text)} | {scores.bm25:.3f} "
        f"| {scores.bm25_stopwords:.3f} | {scores.cosine:.4f} | {scores.bm25_top or '—'} "
        f"| {scores.cosine_top} | {verdict(result.degraded)} | {verdict(result.hybrid)} |"
    )


# --------------------------------------------------------------------------- helpers


def _tokenizer_json(result: TokenizerResult) -> dict[str, object]:
    return {
        "degraded": _choice_json(result.degraded),
        "hybrid": _choice_json(result.hybrid),
        "degraded_confusion": _confusion_json(result.degraded_confusion),
        "hybrid_confusion": _confusion_json(result.hybrid_confusion),
        "legit_weakest_bm25": _round(result.legit_weakest_bm25),
        "off_strongest_bm25": _round(result.off_strongest_bm25),
        "margin": _round(result.margin),
        "relative_margin": (
            None if result.relative_margin is None else _round(result.relative_margin)
        ),
        "kit_only_degraded": _choice_json(result.kit_only_degraded),
    }


def _choice_json(choice: GateChoice) -> dict[str, object]:
    return {
        "bm25": _round(choice.bm25.value),
        "bm25_interval": [_round(choice.bm25.low), _round(choice.bm25.high)],
        "cosine": None if choice.cosine is None else _round(choice.cosine.value),
        "cosine_interval": (
            None
            if choice.cosine is None
            else [_round(choice.cosine.low), _round(choice.cosine.high)]
        ),
        "off_topic_rejected": choice.rejected_off_topic,
        "relaxed": choice.relaxed,
        "legitimate_rejected": list(choice.missed),
        "tied_cells": choice.tied_cells,
    }


def _confusion_json(confusion: Confusion) -> dict[str, int]:
    return {
        "kit_answered": confusion.kit_answered,
        "kit_total": confusion.kit_total,
        "short_legit_answered": confusion.short_answered,
        "short_legit_total": confusion.short_total,
        "off_topic_rejected": confusion.off_rejected,
        "off_topic_total": confusion.off_total,
        "hard_rejected": confusion.hard_rejected,
        "hard_total": confusion.hard_total,
    }


def _cosine(choice: GateChoice) -> float:
    if choice.cosine is None:
        raise ValueError("a degraded gate has no cosine threshold")
    return choice.cosine.value


def _count(calibration: Calibration, group: Group) -> int:
    return sum(s.query.group == group for s in calibration.scored)


def _spread(values: Sequence[float]) -> str:
    if not values:
        return "—"
    summary: Spread = spread(values)
    return f"{summary.minimum:.3f} / {summary.median:.3f} / {summary.maximum:.3f}"


def _listing(items: Sequence[QueryScores]) -> str:
    return "; ".join(
        f"{s.query.id} «{_short(s.query.text)}» (BM25 {s.bm25:.2f}, coseno {s.cosine:.3f})"
        for s in items
    )


def _names(calibration: Calibration, ids: Sequence[str]) -> str:
    if not ids:
        return "ninguna"
    by_id = {s.query.id: s for s in calibration.scored}
    return ", ".join(f"{i} «{_short(by_id[i].query.text)}»" for i in ids)


def _short(text: str) -> str:
    clean = text.replace("|", "/")
    return clean if len(clean) <= _SHORT else clean[: _SHORT - 1].rstrip() + "…"


def _relative(result: TokenizerResult) -> str:
    margin = result.relative_margin
    return "indefinido (el puntaje legítimo más débil es 0)" if margin is None else f"{margin:+.3f}"


def _yes(flag: bool) -> str:
    return "sí" if flag else "no"


def _round(value: float) -> float:
    return round(value, _DECIMALS) + 0.0
