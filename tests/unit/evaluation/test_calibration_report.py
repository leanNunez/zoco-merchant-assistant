"""The calibration files, rendered from scores written by hand so every cell is known."""

import json

from evaluation.retrieval.calibration import (
    Calibration,
    CalibrationQuery,
    Group,
    KitRetrieval,
    QueryScores,
    calibrate,
)
from evaluation.retrieval.calibration_report import CalibrationProvenance, to_json, to_markdown

PROVENANCE = CalibrationProvenance("text-embedding-3-small", "document", 12, "abc123", 0)


def point(
    query_id: str, group: Group, bm25: float, cosine: float, *, hard: bool = False
) -> QueryScores:
    query = CalibrationQuery(query_id, group, f"consulta {query_id}", hard=hard)
    return QueryScores(query, bm25, bm25 / 2, cosine, "DOC-01", "DOC-02", bm25)


def calibration() -> Calibration:
    scored = [
        point("Q01", "kit", 10.0, 0.8),
        point("L01", "short_legit", 4.0, 0.6),
        point("L02", "short_legit", 1.0, 0.5),
        point("N01", "off_topic", 0.5, 0.1),
        point("N02", "off_topic", 3.0, 0.2),
        point("N03", "off_topic", 2.0, 0.55, hard=True),
    ]
    return calibrate(scored, KitRetrieval(0.975, 1.0), KitRetrieval(0.975, 1.0), chunk_count=12)


def markdown() -> str:
    return to_markdown(calibration(), PROVENANCE)


def test_the_rules_are_written_before_any_number() -> None:
    text = markdown()

    assert text.index("## Reglas declaradas antes de medir") < text.index(
        "## Los puntajes por grupo"
    )
    assert "no_data` si BM25 < τ_b **y** coseno < τ_v" in text


def test_the_provenance_has_no_timestamp_and_counts_the_groups() -> None:
    text = markdown()

    assert "- Consultas: 1 del kit, 2 cortas legítimas y 3 fuera de tema, 1 de ellas `hard`" in text
    assert "- Llamadas a la API de embeddings durante esta corrida: 0" in text
    assert "`abc123`" in text
    assert "2026-" not in text


def test_the_group_table_shows_minimum_median_and_maximum_per_signal() -> None:
    text = markdown()

    # short_legit: BM25 4 and 1 -> 1.000 / 2.500 / 4.000; cosine 0.6 and 0.5.
    assert (
        "| short_legit (frases cortas legítimas) | 2 | 1.000 / 2.500 / 4.000 "
        "| 0.500 / 1.250 / 2.000 | 0.500 / 0.550 / 0.600 |"
    ) in text
    assert "|   de ellas `hard` | 1 | 2.000 / 2.000 / 2.000 |" in text


def test_the_overlap_table_names_the_weakest_legitimate_and_the_strongest_off_topic() -> None:
    text = markdown()

    # BM25: weakest legitimate L02 (1.0), strongest off-topic N02 (3.0): they overlap by 2.
    assert "| BM25 (por defecto) | 1.000 (L02) | 3.000 (N02) | -2.000 | -2.000 | 2 | 1 |" in text


def test_the_chosen_thresholds_and_their_cells_are_in_the_report() -> None:
    text = markdown()

    assert (
        "| tokenizador por defecto (seleccionado) | degradada | 0.75 (0.500, 1.000] | — | 1 |"
        in text
    )
    assert "**Seleccionado para el agente: tokenizador por defecto.**" in text
    assert "`no_data` si BM25 < 0.75." in text


def test_every_query_has_a_row_with_its_decisions_and_a_mark_when_the_gate_is_wrong() -> None:
    text = markdown()

    assert (
        "| N02 | off_topic | consulta N02 | 3.000 | 1.500 | 0.2000 | DOC-01 | DOC-02 | Responde ✗ |"
        in text
    )
    assert (
        "| N01 | off_topic | consulta N01 | 0.500 | 0.250 | 0.1000 | DOC-01 | DOC-02 | Rechaza |"
        in text
    )
    assert "| N03 | off_topic (hard) |" in text


def test_the_stopword_decision_says_which_condition_failed() -> None:
    text = markdown()

    assert (
        "| (i) recall@3 de `hybrid+meta(demote)` sobre el kit | 0.975 | 0.975 | sí (no baja) |"
        in text
    )
    assert "Resultado: **no se adopta: se mantiene el tokenizador por defecto**." in text


def test_the_report_states_its_own_limits() -> None:
    text = markdown()

    assert "los escribió quien escribió el sistema" in text
    assert "sobreestiman lo que pasaría con otras" in text


def test_the_json_holds_the_thresholds_the_agent_copies() -> None:
    data = json.loads(to_json(calibration(), PROVENANCE))

    assert data["selected_tokenizer"] == "default"
    assert set(data["thresholds"]) == {"bm25_degraded", "bm25_hybrid", "cosine_hybrid"}
    assert data["thresholds"]["bm25_degraded"] == 0.75
    assert data["stopwords"] == {
        "adopted": False,
        "recall_not_lower": True,
        "mrr_not_lower": True,
        "margin_improves": False,
    }
    assert data["provenance"]["queries"] == {"kit": 1, "short_legit": 2, "off_topic": 3}
    assert data["tokenizers"]["default"]["degraded_confusion"]["off_topic_rejected"] == 1


def test_the_files_are_reproducible() -> None:
    assert markdown() == markdown()
    assert to_json(calibration(), PROVENANCE) == to_json(calibration(), PROVENANCE)
