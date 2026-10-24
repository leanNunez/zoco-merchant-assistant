"""Calibration of the no-data gate of `buscar_documentacion`, as pure functions.

The gate says "the documentation has nothing on this" and is what moves a conversation to SUGGEST
(`low_confidence`). A false `no_data` is safe but useless; a false `ok` only lets the model answer
from loosely related passages. The thresholds are therefore chosen by a rule written BEFORE the
numbers were seen (`GATE_RULES`, also in ADR-0007) and applied mechanically:

- the signals are the best BM25 score of the query over the chunks and the best cosine similarity
  of the query vector against the chunk vectors;
- hybrid gate: `no_data` iff BOTH are weak (`bm25 < tau_b` and `cosine < tau_v`); degraded gate
  (no embeddings): `no_data` iff `bm25 < tau_b_degraded`;
- the thresholds keep every legitimate query answerable, then reject as many off-topic queries as
  possible, then sit in the middle of what is left.

Nothing here reads a file except `load_calibration_queries`, and nothing calls a provider: the
caller passes the embedder (the cached one).
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from merchant_assistant.knowledge.bm25 import BM25Index
from merchant_assistant.knowledge.chunking import ChunkingStrategy, chunk_corpus
from merchant_assistant.knowledge.documents import Document
from merchant_assistant.knowledge.errors import QuestionFormatError
from merchant_assistant.knowledge.ports import Embedder
from merchant_assistant.knowledge.questions import Question
from merchant_assistant.knowledge.text import Tokenizer
from merchant_assistant.knowledge.vector import VectorIndex

Group = Literal["kit", "short_legit", "off_topic"]
GROUPS: tuple[Group, ...] = ("kit", "short_legit", "off_topic")
TokenizerName = Literal["default", "stopwords"]
TOKENIZERS: tuple[TokenizerName, ...] = ("default", "stopwords")

# The chunking of the agent's search (the one the pre-declared selection of the retrieval
# evaluation picked) and the one it replaces, kept to measure the old floor on the same queries.
AGENT_CHUNKING = ChunkingStrategy("document")
PREVIOUS_CHUNKING = ChunkingStrategy("section")
PREVIOUS_BM25_FLOOR = 5.25  # the floor calibrated on `section` chunking and the 20 kit questions

GATE_RULES = (
    "1. Los conjuntos de consultas se escribieron antes de medir y no se modifican después de ver "
    "los puntajes: `kit` (las 20 preguntas de `questions.jsonl`, referenciadas), `short_legit` "
    "(frases cortas y naturales que el corpus responde) y `off_topic` (fuera del dominio; las "
    "marcadas `hard` comparten palabras genéricas con el dominio).",
    "2. Señales: el mejor puntaje BM25 de la consulta sobre los chunks `document` y el mejor "
    "coseno entre el vector de la consulta y los de los chunks.",
    "3. Compuerta híbrida: `no_data` si BM25 < τ_b **y** coseno < τ_v. Compuerta degradada (sin "
    "embeddings, solo existe BM25): `no_data` si BM25 < τ_b_degradado.",
    "4. Selección de los umbrales: (a) toda consulta de `kit` y de `short_legit` sigue "
    "respondiendo; (b) entre las compuertas que cumplen (a), la que rechaza más consultas "
    "`off_topic`; (c) entre empates, el centro del intervalo factible. Un umbral solo importa "
    "por entre qué dos puntajes observados cae, así que cada eje se parte en celdas y el umbral es "
    "el punto medio de la celda elegida. En dos ejes, el centro es el punto medio del rango que "
    "ocupan las celdas empatadas en cada eje; si esa celda no está empatada, se toma la empatada "
    "más cercana (distancia L1 con cada eje normalizado por su rango).",
    "5. Relajación: si (a) solo se cumple con una compuerta que no rechaza ninguna consulta "
    "`off_topic`, se relaja lo justo para conservar las 20 del `kit`, se aplican de nuevo (b) y "
    "(c) y se informan por nombre las `short_legit` que quedan rechazadas.",
    "6. Tokenizador con stopwords en la búsqueda del agente: solo si (i) recall@3 y MRR de "
    "`hybrid+meta(demote)` sobre las 20 preguntas del kit no bajan respecto del tokenizador por "
    "defecto **y** (ii) sube el margen factible de BM25, definido como (L - U) / L con L el menor "
    "mejor-BM25 de las consultas legítimas (kit y short_legit) y U el mayor de las `off_topic`. Se "
    "normaliza por L porque quitar palabras funcionales cambia la escala de BM25; el margen "
    "absoluto se informa al lado. Si falla (i) o (ii), se mantiene el tokenizador por defecto.",
)

LIMITS = (
    "Los tres conjuntos son chicos (20, 30 y 45 consultas) y los escribió quien escribió el "
    "sistema: no son una muestra de lo que van a preguntar personas reales, y los umbrales se "
    "eligieron sobre estas mismas consultas, así que sobreestiman lo que pasaría con otras.",
    "Una consulta legítima distinta de las medidas puede caer debajo de la compuerta (falla "
    "segura: pasa a SUGGEST) y una fuera de tema puede pasarla (falla tolerable: el modelo recibe "
    "pasajes poco relacionados y tiene la instrucción de no inventar). El diseño prefiere la "
    "segunda.",
    "El corpus son 12 documentos con pocos cientos de palabras: los puntajes BM25 dependen del "
    "tamaño del corpus (el IDF) y los cosenos de `text-embedding-3-small`. Cambiar el corpus o el "
    "modelo exige volver a calibrar (`make eval-retrieval`).",
    "Las consultas fuera de tema que comparten palabras con el dominio (`hard`) son el caso "
    "difícil de verdad: ningún umbral sobre estas señales separa 'habla de entradas' de 'habla de "
    "las entradas de ZocoTickets'. La defensa para esos casos es el prompt, no la compuerta.",
)

_CELL_DIGITS = (3, 4, 6, 9)  # thresholds are rounded to the first precision that stays in a cell


@dataclass(frozen=True, slots=True)
class CalibrationQuery:
    id: str
    group: Group
    text: str
    hard: bool = False
    expected: frozenset[str] = frozenset()


def load_calibration_queries(path: Path, kit: Sequence[Question]) -> tuple[CalibrationQuery, ...]:
    """The queries of `calibration_queries.jsonl`; the `kit` row expands to the kit questions.

    The file holds one `{"group": "kit", "source": "questions.jsonl"}` row, which stands for
    `kit` (so the 20 questions are referenced and never copied), and one row per other query:
    `id`, `group`, `text`, and optionally `expected` (document ids) and `hard`.
    """
    queries: list[CalibrationQuery] = []
    kit_seen = False
    lines = path.read_text(encoding="utf-8").splitlines()
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            raise QuestionFormatError(f"{path.name}:{number}: not valid JSON") from None
        if not isinstance(row, dict):
            raise QuestionFormatError(f"{path.name}:{number}: expected a JSON object")
        group = row.get("group")
        if group == "kit":
            if kit_seen or row.get("source") != "questions.jsonl":
                raise QuestionFormatError(f"{path.name}:{number}: one kit row naming its source")
            kit_seen = True
            queries.extend(CalibrationQuery(q.id, "kit", q.text, expected=q.relevant) for q in kit)
            continue
        queries.append(_parse(row, path.name, number))
    if not kit_seen:
        raise QuestionFormatError(f"{path.name}: the kit row is missing")
    ids = [query.id for query in queries]
    if len(set(ids)) != len(ids):
        raise QuestionFormatError(f"{path.name}: repeated query ids")
    return tuple(queries)


def _parse(row: dict[str, object], name: str, number: int) -> CalibrationQuery:
    group, text, query_id = row.get("group"), row.get("text"), row.get("id")
    if group not in ("short_legit", "off_topic"):
        raise QuestionFormatError(f"{name}:{number}: unknown group")
    if not isinstance(text, str) or not text.strip() or not isinstance(query_id, str):
        raise QuestionFormatError(f"{name}:{number}: a query needs an id and a text")
    expected = row.get("expected", [])
    if not isinstance(expected, list) or not all(isinstance(doc, str) for doc in expected):
        raise QuestionFormatError(f"{name}:{number}: expected must be a list of document ids")
    if group == "off_topic" and expected:
        raise QuestionFormatError(f"{name}:{number}: an off-topic query has no expected document")
    hard = row.get("hard", False)
    if not isinstance(hard, bool) or (hard and group != "off_topic"):
        raise QuestionFormatError(f"{name}:{number}: only off-topic queries can be hard")
    return CalibrationQuery(query_id, group, text, hard, frozenset(expected))


def calibration_texts(queries: Sequence[CalibrationQuery]) -> list[str]:
    """The texts `make index` must embed so the calibration runs without the API."""
    return [query.text for query in queries]


# --------------------------------------------------------------------------- scores


@dataclass(frozen=True, slots=True)
class QueryScores:
    query: CalibrationQuery
    bm25: float  # best BM25 over the chunks, default tokenizer
    bm25_stopwords: float  # the same with the stopword tokenizer
    cosine: float  # best cosine of the query vector against the chunk vectors
    bm25_top: str  # the document of the best BM25 chunk ("" when nothing matches)
    cosine_top: str
    previous_bm25: float  # on the previous chunking, to evaluate the previous floor

    def bm25_of(self, tokenizer: TokenizerName) -> float:
        return self.bm25 if tokenizer == "default" else self.bm25_stopwords


def score_queries(
    documents: Sequence[Document],
    queries: Sequence[CalibrationQuery],
    embedder: Embedder,
    chunking: ChunkingStrategy = AGENT_CHUNKING,
) -> tuple[QueryScores, ...]:
    chunks = chunk_corpus(documents, chunking)
    previous = chunk_corpus(documents, PREVIOUS_CHUNKING)
    plain = BM25Index(chunks, Tokenizer())
    stopped = BM25Index(chunks, Tokenizer(stopwords=True))
    before = BM25Index(previous, Tokenizer())
    vectors = VectorIndex(chunks, embedder.embed([chunk.index_text for chunk in chunks]))
    query_vectors = embedder.embed([query.text for query in queries])
    scored: list[QueryScores] = []
    for query, vector in zip(queries, query_vectors, strict=True):
        lexical = plain.search(query.text, 1)
        nearest = vectors.search(vector, 1)[0]
        scored.append(
            QueryScores(
                query=query,
                bm25=max(plain.scores(query.text), default=0.0),
                bm25_stopwords=max(stopped.scores(query.text), default=0.0),
                cosine=nearest.score,
                bm25_top=lexical[0].doc_id if lexical else "",
                cosine_top=nearest.doc_id,
                previous_bm25=max(before.scores(query.text), default=0.0),
            )
        )
    return tuple(scored)


# --------------------------------------------------------------------------- gate selection


@dataclass(frozen=True, slots=True)
class Axis:
    """A threshold, the interval of equivalent thresholds it was taken from, and its value."""

    value: float
    low: float  # exclusive: the largest observed score the threshold rejects
    high: float  # inclusive: the smallest observed score it keeps


@dataclass(frozen=True, slots=True)
class GateChoice:
    bm25: Axis
    cosine: Axis | None  # None for the degraded gate
    rejected_off_topic: int
    relaxed: bool  # (a) was relaxed to the kit alone (rule 5)
    missed: tuple[str, ...]  # legitimate queries (ids) the chosen gate rejects
    tied_cells: int  # how many cells reach the maximum


def _cells(values: Sequence[float]) -> list[tuple[float, float]]:
    """Intervals of equivalent thresholds `(low, high]` over the sorted distinct `values`.

    Cell 0 is "reject nothing" and is represented by `(values[0], values[0])`; cell `i >= 1`
    rejects exactly the values `<= values[i - 1]` and keeps the rest.
    """
    ordered = sorted(set(values))
    return [
        (ordered[0], ordered[0]),
        *((ordered[i - 1], ordered[i]) for i in range(1, len(ordered))),
    ]


def _value(low: float, high: float) -> float:
    """The threshold of a cell: its middle, rounded as little as the cell allows."""
    if low == high:
        return low
    middle = (low + high) / 2
    for digits in _CELL_DIGITS:
        rounded = round(middle, digits)
        if low < rounded <= high:
            return rounded
    return middle


def choose_gate(
    scored: Sequence[QueryScores], tokenizer: TokenizerName, *, hybrid: bool
) -> GateChoice:
    """Apply rules 3 to 5 to the scores; `hybrid=False` is the degraded (BM25 only) gate."""
    must_keep = [s for s in scored if s.query.group in ("kit", "short_legit")]
    choice = _best(scored, must_keep, tokenizer, hybrid=hybrid, relaxed=False)
    if choice.rejected_off_topic > 0:
        return choice
    kit_only = [s for s in scored if s.query.group == "kit"]
    relaxed = _best(scored, kit_only, tokenizer, hybrid=hybrid, relaxed=True)
    return relaxed if relaxed.rejected_off_topic > 0 else choice


def _best(
    scored: Sequence[QueryScores],
    must_keep: Sequence[QueryScores],
    tokenizer: TokenizerName,
    *,
    hybrid: bool,
    relaxed: bool,
) -> GateChoice:
    b_cells = _cells([s.bm25_of(tokenizer) for s in scored])
    c_cells = _cells([s.cosine for s in scored]) if hybrid else [(0.0, 0.0)]
    off = [s for s in scored if s.query.group == "off_topic"]

    def rejects(s: QueryScores, i: int, j: int) -> bool:
        weak_lexical = s.bm25_of(tokenizer) <= b_cells[i][0] and i > 0
        if not hybrid:
            return weak_lexical
        return weak_lexical and s.cosine <= c_cells[j][0] and j > 0

    results: list[tuple[int, int, int]] = []
    for i in range(len(b_cells)):
        for j in range(len(c_cells)):
            if any(rejects(s, i, j) for s in must_keep):
                continue
            results.append((i, j, sum(rejects(s, i, j) for s in off)))
    top = max(count for _, _, count in results)
    # A gate that rejects nothing is the cell (0, 0), not the middle of a region of them.
    tied = [(i, j) for i, j, count in results if count == top] if top else [(0, 0)]
    i, j = _middle(tied, b_cells, c_cells, hybrid=hybrid)
    chosen_b = Axis(_value(*b_cells[i]), *b_cells[i])
    chosen_c = Axis(_value(*c_cells[j]), *c_cells[j]) if hybrid else None
    missed = tuple(
        s.query.id for s in scored if s.query.group in ("kit", "short_legit") and rejects(s, i, j)
    )
    return GateChoice(chosen_b, chosen_c, top, relaxed, missed, len(tied))


def _middle(
    tied: Sequence[tuple[int, int]],
    b_cells: Sequence[tuple[float, float]],
    c_cells: Sequence[tuple[float, float]],
    *,
    hybrid: bool,
) -> tuple[int, int]:
    """The tied cell at the middle of the feasible region (rule 4.c)."""
    if len(tied) == 1:
        return tied[0]
    b_low = min(b_cells[i][0] for i, _ in tied)
    b_high = max(b_cells[i][1] for i, _ in tied)
    c_low = min(c_cells[j][0] for _, j in tied)
    c_high = max(c_cells[j][1] for _, j in tied)
    target_b = (b_low + b_high) / 2
    target_c = (c_low + c_high) / 2

    def distance(cell: tuple[int, int]) -> tuple[float, int, int]:
        i, j = cell
        mid_b = (b_cells[i][0] + b_cells[i][1]) / 2
        mid_c = (c_cells[j][0] + c_cells[j][1]) / 2
        d = abs(mid_b - target_b) / ((b_high - b_low) or 1.0)
        if hybrid:
            d += abs(mid_c - target_c) / ((c_high - c_low) or 1.0)
        return (d, i, j)

    return min(tied, key=distance)


def answers(scores: QueryScores, choice: GateChoice, tokenizer: TokenizerName) -> bool:
    """Whether the gate of `choice` lets the query through (does not say `no_data`)."""
    weak = scores.bm25_of(tokenizer) < choice.bm25.value
    if choice.cosine is None:
        return not weak
    return not (weak and scores.cosine < choice.cosine.value)


# --------------------------------------------------------------------------- the whole result


@dataclass(frozen=True, slots=True)
class Spread:
    """Min, median and max of one signal over one group of queries."""

    minimum: float
    median: float
    maximum: float


@dataclass(frozen=True, slots=True)
class Confusion:
    kit_answered: int
    kit_total: int
    short_answered: int
    short_total: int
    off_rejected: int
    off_total: int
    hard_rejected: int
    hard_total: int


@dataclass(frozen=True, slots=True)
class TokenizerResult:
    name: TokenizerName
    degraded: GateChoice
    hybrid: GateChoice
    degraded_confusion: Confusion
    hybrid_confusion: Confusion
    legit_weakest_bm25: float
    off_strongest_bm25: float
    kit_only_degraded: GateChoice  # informational: rule 4 asking only that the kit answers

    @property
    def margin(self) -> float:
        """Absolute margin of the BM25 signal: weakest legitimate minus strongest off-topic."""
        return self.legit_weakest_bm25 - self.off_strongest_bm25

    @property
    def relative_margin(self) -> float | None:
        """The margin as a fraction of the weakest legitimate score (rule 6.ii).

        Undefined (None) when that score is 0: a legitimate query that shares no token with the
        corpus has no lexical signal to normalize by, and an undefined margin never "improves".
        """
        return self.margin / self.legit_weakest_bm25 if self.legit_weakest_bm25 else None


@dataclass(frozen=True, slots=True)
class KitRetrieval:
    """recall@3 and MRR of `hybrid+meta(demote)` on the 20 kit questions (rule 6.i)."""

    recall_at_3: float
    mrr: float


@dataclass(frozen=True, slots=True)
class StopwordDecision:
    adopted: bool
    recall_not_lower: bool
    mrr_not_lower: bool
    margin_improves: bool
    default_kit: KitRetrieval
    stopwords_kit: KitRetrieval


@dataclass(frozen=True, slots=True)
class Calibration:
    scored: tuple[QueryScores, ...]
    results: dict[TokenizerName, TokenizerResult]
    decision: StopwordDecision
    selected: TokenizerName
    previous_floor_misses: tuple[str, ...]  # legitimate queries the previous floor rejected
    previous_floor_off_accepted: int
    chunk_count: int

    @property
    def chosen(self) -> TokenizerResult:
        return self.results[self.selected]


def spread(values: Sequence[float]) -> Spread:
    return Spread(min(values), statistics.median(values), max(values))


def calibrate(
    scored: Sequence[QueryScores],
    default_kit: KitRetrieval,
    stopwords_kit: KitRetrieval,
    chunk_count: int,
) -> Calibration:
    results = {name: _tokenizer_result(scored, name) for name in TOKENIZERS}
    default, stopwords = results["default"], results["stopwords"]
    recall_ok = _not_lower(stopwords_kit.recall_at_3, default_kit.recall_at_3)
    mrr_ok = _not_lower(stopwords_kit.mrr, default_kit.mrr)
    margin_ok = _margin_improves(stopwords.relative_margin, default.relative_margin)
    adopted = recall_ok and mrr_ok and margin_ok
    decision = StopwordDecision(adopted, recall_ok, mrr_ok, margin_ok, default_kit, stopwords_kit)
    legit = [s for s in scored if s.query.group in ("kit", "short_legit")]
    off = [s for s in scored if s.query.group == "off_topic"]
    return Calibration(
        scored=tuple(scored),
        results=results,
        decision=decision,
        selected="stopwords" if adopted else "default",
        previous_floor_misses=tuple(
            s.query.id for s in legit if s.previous_bm25 < PREVIOUS_BM25_FLOOR
        ),
        previous_floor_off_accepted=sum(s.previous_bm25 >= PREVIOUS_BM25_FLOOR for s in off),
        chunk_count=chunk_count,
    )


def _margin_improves(candidate: float | None, reference: float | None) -> bool:
    if candidate is None:
        return False
    return reference is None or round(candidate, 12) > round(reference, 12)


def _not_lower(candidate: float, reference: float) -> bool:
    return round(candidate, 12) >= round(reference, 12)


def _tokenizer_result(scored: Sequence[QueryScores], name: TokenizerName) -> TokenizerResult:
    degraded = choose_gate(scored, name, hybrid=False)
    hybrid = choose_gate(scored, name, hybrid=True)
    legit = [s.bm25_of(name) for s in scored if s.query.group in ("kit", "short_legit")]
    off = [s.bm25_of(name) for s in scored if s.query.group == "off_topic"]
    return TokenizerResult(
        name=name,
        degraded=degraded,
        hybrid=hybrid,
        degraded_confusion=confusion(scored, degraded, name),
        hybrid_confusion=confusion(scored, hybrid, name),
        legit_weakest_bm25=min(legit),
        off_strongest_bm25=max(off),
        kit_only_degraded=_best(
            scored,
            [s for s in scored if s.query.group == "kit"],
            name,
            hybrid=False,
            relaxed=True,
        ),
    )


def confusion(scored: Sequence[QueryScores], choice: GateChoice, name: TokenizerName) -> Confusion:
    def kept(group: Group) -> list[bool]:
        return [answers(s, choice, name) for s in scored if s.query.group == group]

    hard = [answers(s, choice, name) for s in scored if s.query.hard]
    off = kept("off_topic")
    return Confusion(
        kit_answered=sum(kept("kit")),
        kit_total=len(kept("kit")),
        short_answered=sum(kept("short_legit")),
        short_total=len(kept("short_legit")),
        off_rejected=len(off) - sum(off),
        off_total=len(off),
        hard_rejected=len(hard) - sum(hard),
        hard_total=len(hard),
    )
