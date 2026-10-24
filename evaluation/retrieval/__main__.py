"""CLI: `python -m evaluation.retrieval index` embeds; `... run` measures and saves;
`... calibrate` calibrates the no-data gate of the agent's search.

A thin layer: it loads the kit, takes the cached embedder from the composition root and
writes what the pure functions return. Messages for the person running it are Spanish
and one line; provider details are never printed, because an SDK message can quote part
of the API key.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pydantic import ValidationError

from evaluation.retrieval.calibration import (
    KitRetrieval,
    calibrate,
    calibration_texts,
    load_calibration_queries,
    score_queries,
)
from evaluation.retrieval.calibration_report import CalibrationProvenance
from evaluation.retrieval.calibration_report import to_json as calibration_json
from evaluation.retrieval.calibration_report import to_markdown as calibration_markdown
from evaluation.retrieval.pipeline import (
    GRID,
    REFERENCE_DATE,
    RetrievalEvaluation,
    run_evaluation,
    texts_to_index,
)
from evaluation.retrieval.provenance import Provenance, corpus_digest
from evaluation.retrieval.report import to_json, to_markdown
from merchant_assistant.bootstrap import build_embedder
from merchant_assistant.knowledge.adapters.filesystem import load_documents, load_questions
from merchant_assistant.knowledge.chunking import chunk_corpus
from merchant_assistant.knowledge.embedding import CachedEmbedder
from merchant_assistant.knowledge.errors import EmbeddingsUnavailableError
from merchant_assistant.shared.config import Settings, get_settings
from merchant_assistant.shared.errors import DomainError

REPOSITORY = Path(__file__).resolve().parents[2]
DOCS = REPOSITORY / "docs"
QUESTIONS = REPOSITORY / "questions.jsonl"
CALIBRATION_QUERIES = REPOSITORY / "evaluation" / "retrieval" / "calibration_queries.jsonl"
RESULTS = REPOSITORY / "evaluation" / "results" / "retrieval"


def main(
    argv: list[str] | None = None,
    *,
    settings: Settings | None = None,
    results_dir: Path = RESULTS,
) -> int:
    parser = argparse.ArgumentParser(prog="python -m evaluation.retrieval")
    parser.add_argument("command", choices=["index", "run", "calibrate"])
    command = parser.parse_args(argv).command
    try:
        settings = settings or get_settings()
    except ValidationError as error:
        first = error.errors()[0]
        print(f"Configuración inválida en {first['loc'][0]}: {first['msg']}", file=sys.stderr)
        return 2
    try:
        embedder = build_embedder(settings)
        if command == "index":
            return index(embedder)
        if command == "calibrate":
            return calibrate_gate(embedder, settings, results_dir)
        return run(embedder, settings, results_dir)
    except EmbeddingsUnavailableError:
        print(_unavailable_message(settings), file=sys.stderr)
        return 1
    except DomainError as error:
        print(f"No se pudo completar la evaluación ({error.code}).", file=sys.stderr)
        return 1


def index_texts() -> list[str]:
    """Everything the evaluation and the calibration embed: chunks, kit questions, queries."""
    documents = load_documents(DOCS)
    questions = load_questions(QUESTIONS)
    queries = load_calibration_queries(CALIBRATION_QUERIES, questions)
    return [*texts_to_index(documents, questions), *calibration_texts(queries)]


def index(embedder: CachedEmbedder) -> int:
    texts = index_texts()
    embedder.embed(texts)
    print(
        f"Textos: {len(texts)} ({embedder.hits + embedder.misses} distintos). "
        f"En caché: {embedder.hits}. Embebidos ahora: {embedder.misses}. "
        f"Llamadas a la API: {embedder.inner_calls}."
    )
    return 0


def run(embedder: CachedEmbedder, settings: Settings, results_dir: Path) -> int:
    documents = load_documents(DOCS)
    questions = load_questions(QUESTIONS)
    evaluation = run_evaluation(documents, questions, embedder)
    files = [
        (path.relative_to(REPOSITORY).as_posix(), path.read_bytes())
        for path in [*sorted(DOCS.glob("*.md")), QUESTIONS]
    ]
    provenance = Provenance(
        embeddings_model=settings.embeddings_model,
        reference_date=REFERENCE_DATE,
        corpus_sha256=corpus_digest(files),
        chunk_counts=evaluation.chunk_counts,
        api_calls=embedder.inner_calls,
    )
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / "metrics.json").write_text(
        to_json(evaluation, provenance, questions), encoding="utf-8"
    )
    (results_dir / "report.md").write_text(
        to_markdown(evaluation, provenance, questions), encoding="utf-8"
    )
    print(
        f"Resultados en {results_dir}. Chunking seleccionado: "
        f"{evaluation.selection.selected.label}. Llamadas a la API: {embedder.inner_calls}."
    )
    if embedder.inner_calls:
        print(
            "Aviso: la caché estaba incompleta y hubo que llamar a la API; corré "
            "`make index` y repetí para obtener resultados reproducibles sin red.",
            file=sys.stderr,
        )
    return 0


def calibrate_gate(embedder: CachedEmbedder, settings: Settings, results_dir: Path) -> int:
    documents = load_documents(DOCS)
    questions = load_questions(QUESTIONS)
    queries = load_calibration_queries(CALIBRATION_QUERIES, questions)
    evaluation = run_evaluation(documents, questions, embedder)
    chunking = next(g for g in GRID if g.label == evaluation.selection.selected.label)
    scored = score_queries(documents, queries, embedder, chunking)
    default_kit, stopwords_kit = kit_retrieval(evaluation)
    calibration = calibrate(
        scored, default_kit, stopwords_kit, len(chunk_corpus(documents, chunking))
    )
    files = [
        (path.relative_to(REPOSITORY).as_posix(), path.read_bytes())
        for path in [*sorted(DOCS.glob("*.md")), QUESTIONS, CALIBRATION_QUERIES]
    ]
    provenance = CalibrationProvenance(
        embeddings_model=settings.embeddings_model,
        chunking=chunking.label,
        chunks=len(chunk_corpus(documents, chunking)),
        corpus_sha256=corpus_digest(files),
        api_calls=embedder.inner_calls,
    )
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / "gate_calibration.json").write_text(
        calibration_json(calibration, provenance), encoding="utf-8"
    )
    (results_dir / "gate_calibration.md").write_text(
        calibration_markdown(calibration, provenance), encoding="utf-8"
    )
    chosen = calibration.chosen
    print(
        f"Calibración en {results_dir}. Tokenizador: {calibration.selected}. "
        f"Umbrales: BM25 degradado {chosen.degraded.bm25.value:g}, híbrido BM25 "
        f"{chosen.hybrid.bm25.value:g} y coseno "
        f"{chosen.hybrid.cosine.value if chosen.hybrid.cosine else 0:g}. "
        f"Llamadas a la API: {embedder.inner_calls}."
    )
    if embedder.inner_calls:
        print(
            "Aviso: la caché estaba incompleta y hubo que llamar a la API; corré "
            "`make index` y repetí para obtener resultados reproducibles sin red.",
            file=sys.stderr,
        )
    return 0


def kit_retrieval(evaluation: RetrievalEvaluation) -> tuple[KitRetrieval, KitRetrieval]:
    """recall@3 and MRR of `hybrid+meta(demote)` with the default tokenizer and with stopwords."""
    main = next(row.run for row in evaluation.main if row.run.strategy == "hybrid+meta(demote)")
    stopped = next(
        run
        for run in evaluation.ablations
        if run.strategy == "hybrid+meta(demote)(stopwords)"
        and run.chunking == evaluation.selection.selected.label
    )
    return KitRetrieval(main.recall_at_3, main.mrr), KitRetrieval(stopped.recall_at_3, stopped.mrr)


def _unavailable_message(settings: Settings) -> str:
    if settings.openai_api_key is None:
        return (
            "Faltan embeddings en la caché y no hay OPENAI_API_KEY configurada: "
            "agregala en .env y corré `make index`."
        )
    return (
        "No se pudo usar el proveedor de embeddings (embeddings_unavailable): "
        "revisá la clave, el modelo y la conexión."
    )


if __name__ == "__main__":
    sys.exit(main())
