"""BM25 checked against scores worked out by hand.

Corpus used by most tests (tokens are already normalized, so tokenization is trivial):

    c0 = "casa gato gato perro"   dl = 4
    c1 = "casa perro pez"         dl = 3
    c2 = "casa pez loro"          dl = 3

    N = 3 chunks, total tokens = 10, avgdl = 10/3

Document frequencies and Lucene-style IDF, idf(t) = ln(1 + (N - n_t + 0.5) / (n_t + 0.5)):

    casa:  n = 3 -> ln(1 + 0.5/3.5) = ln(8/7) = 0.1335313926   (in every chunk, still > 0)
    gato:  n = 1 -> ln(1 + 2.5/1.5) = ln(8/3) = 0.9808292530
    perro: n = 2 -> ln(1 + 1.5/2.5) = ln(8/5) = 0.4700036292
    pez:   n = 2 -> ln(8/5)                   = 0.4700036292
    loro:  n = 1 -> ln(8/3)                   = 0.9808292530

With k1 = 1.2 and b = 0.75 the term-frequency part is
tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * dl / avgdl)):

    dl = 4: 0.75 * 4 / (10/3) = 0.9    -> length factor 1.2 * 1.15 = 1.38
    dl = 3: 0.75 * 3 / (10/3) = 0.675  -> length factor 1.2 * 0.925 = 1.11

    tf = 2, dl = 4: 2 * 2.2 / (2 + 1.38) = 4.4 / 3.38 = 1.3017751479
    tf = 1, dl = 4: 2.2 / (1 + 1.38)     = 2.2 / 2.38 = 0.9243697479
    tf = 1, dl = 3: 2.2 / (1 + 1.11)     = 2.2 / 2.11 = 1.0426540284
"""

import pytest

from merchant_assistant.knowledge.bm25 import BM25Index
from merchant_assistant.knowledge.chunking import Chunk
from merchant_assistant.knowledge.text import Tokenizer


def chunk(position: int, text: str) -> Chunk:
    return Chunk(
        id=f"D{position}#0",
        doc_id=f"D{position}",
        ordinal=0,
        heading=None,
        text=text,
        index_text=text,
    )


def corpus(*texts: str) -> list[Chunk]:
    return [chunk(position, text) for position, text in enumerate(texts)]


HAND = corpus("casa gato gato perro", "casa perro pez", "casa pez loro")
CASA_C0 = 0.1234323797  # ln(8/7) * 0.9243697479 = 0.1335313926 * 0.9243697479
CASA_C1_C2 = 0.1392270444  # ln(8/7) * 1.0426540284 = 0.1335313926 * 1.0426540284


def index(chunks: list[Chunk], **parameters: float) -> BM25Index:
    return BM25Index(chunks, Tokenizer(), **parameters)


def test_a_rare_term_scores_idf_times_the_saturated_term_frequency() -> None:
    # "gato" only in c0 with tf = 2: ln(8/3) * 4.4/3.38 = 0.9808292530 * 1.3017751479
    scores = index(HAND).scores("gato")

    assert scores == [pytest.approx(1.276819146, rel=1e-9), 0.0, 0.0]


def test_a_repeated_query_token_counts_once() -> None:
    assert index(HAND).scores("gato gato gato") == index(HAND).scores("gato")
    assert index(HAND).scores("gato gato gato")[0] == pytest.approx(1.276819146, rel=1e-9)


def test_a_term_in_every_chunk_keeps_a_positive_idf_and_favours_shorter_chunks() -> None:
    # "casa": ln(8/7) = 0.1335313926 > 0 although n = N.
    # c0 (dl 4): 0.1335313926 * 0.9243697479 = 0.1234323797
    # c1, c2 (dl 3): 0.1335313926 * 1.0426540284 = 0.1392270444
    scores = index(HAND).scores("casa")

    assert scores == [
        pytest.approx(CASA_C0, rel=1e-9),
        pytest.approx(CASA_C1_C2, rel=1e-9),
        pytest.approx(CASA_C1_C2, rel=1e-9),
    ]


def test_the_scores_of_several_distinct_terms_add_up() -> None:
    # c0: gato 1.276819146 + perro 0.4700036292 * 0.9243697479 (tf 1, dl 4) = 0.4344571...
    #     = 1.276819146 + 0.434457136 = 1.711276282
    # c1: perro 0.4700036292 * 1.0426540284 (tf 1, dl 3) = 0.4900511774
    # c2: no query term -> 0
    scores = index(HAND).scores("gato perro")

    assert scores == [
        pytest.approx(1.711276282, rel=1e-9),
        pytest.approx(0.4900511774, rel=1e-9),
        0.0,
    ]


def test_without_length_normalization_every_chunk_scores_the_idf_of_a_term_it_contains() -> None:
    # b = 0: denominator tf + k1; k1 = 1 and tf = 1 give 1 * 2 / 2 = 1, so score = idf.
    # "casa" in all three chunks: ln(8/7) = 0.1335313926 each.
    scores = index(HAND, k1=1.0, b=0.0).scores("casa")

    assert scores == [pytest.approx(0.1335313926, rel=1e-9)] * 3


def test_with_k1_zero_the_term_frequency_stops_mattering() -> None:
    # k1 = 0: tf * 1 / tf = 1, so "gato" (tf 2) in c0 scores its idf: ln(8/3) = 0.9808292530.
    scores = index(HAND, k1=0.0).scores("gato")

    assert scores == [pytest.approx(0.9808292530, rel=1e-9), 0.0, 0.0]


def test_search_orders_by_score_and_drops_chunks_with_no_matching_term() -> None:
    results = index(HAND).search("gato perro", k=5)

    assert [(r.chunk_id, r.doc_id) for r in results] == [("D0#0", "D0"), ("D1#0", "D1")]
    assert [r.score for r in results] == [
        pytest.approx(1.711276282, rel=1e-9),
        pytest.approx(0.4900511774, rel=1e-9),
    ]


def test_search_never_pads_with_zero_score_chunks() -> None:
    results = index(HAND).search("gato", k=3)

    assert [(r.chunk_id, r.doc_id) for r in results] == [("D0#0", "D0")]
    assert results[0].score == pytest.approx(1.276819146, rel=1e-9)


def test_search_returns_at_most_k_chunks() -> None:
    results = index(HAND).search("casa", k=2)

    assert [r.chunk_id for r in results] == ["D1#0", "D2#0"]


def test_equal_scores_are_ordered_by_corpus_position() -> None:
    # c1 and c2 both score 0.1392270444 for "casa"; c0 scores less. The earlier one comes first.
    results = index(HAND).search("casa", k=3)

    assert [r.chunk_id for r in results] == ["D1#0", "D2#0", "D0#0"]
    assert results[0].score == results[1].score


def test_a_query_with_only_unknown_tokens_finds_nothing() -> None:
    assert index(HAND).search("zzz qqq", k=3) == []
    assert index(HAND).scores("zzz") == [0.0, 0.0, 0.0]


def test_an_empty_query_finds_nothing() -> None:
    assert index(HAND).search("", k=3) == []
    assert index(HAND).search("¿? ...", k=3) == []


def test_an_empty_corpus_finds_nothing_and_does_not_divide_by_zero() -> None:
    empty = index([])

    assert empty.scores("casa") == []
    assert empty.search("casa", k=3) == []


def test_chunks_without_tokens_score_zero_and_do_not_break_the_average() -> None:
    # Only punctuation: dl = 0 for the first chunk, so avgdl = (0 + 1) / 2 = 0.5.
    # "gato" appears once, in the one-token chunk: idf = ln(1 + (2 - 1 + 0.5) / 1.5) = ln 2,
    # tf part = 2.2 / (1 + 1.2 * (0.25 + 0.75 * 1 / 0.5)) = 2.2 / 3.1 = 0.7096774194
    # score = 0.6931471806 * 0.7096774194 = 0.4919109023
    chunks = corpus("... ???", "gato")

    scores = index(chunks).scores("gato")

    assert scores == [0.0, pytest.approx(0.4919109023, rel=1e-9)]
    assert index(chunks).search("gato", k=2)[0].chunk_id == "D1#0"


def test_a_corpus_made_only_of_empty_chunks_finds_nothing() -> None:
    only_empty = index(corpus("...", ""))

    assert only_empty.scores("gato") == [0.0, 0.0]
    assert only_empty.search("gato", k=2) == []


def test_the_query_word_pro_finds_the_chunk_with_the_plan_code_zt_pro_2() -> None:
    chunks = corpus(
        "El plan ZT-PRO-2 exige 400 entradas pagadas por mes.",
        "El plan ZT-START-1 no tiene mínimo mensual.",
    )

    results = index(chunks).search("PRO", k=5)

    assert [r.chunk_id for r in results] == ["D0#0"]


def test_the_exact_plan_code_ranks_above_a_chunk_that_shares_only_a_part_of_it() -> None:
    # Both chunks contain the part "zt"; only the second contains the whole "zt-start-1".
    chunks = corpus("Plan ZT-PRO-2 exige 400.", "Plan ZT-START-1 sin mínimo.")

    results = index(chunks).search("ZT-START-1", k=5)

    assert [r.chunk_id for r in results] == ["D1#0", "D0#0"]


def test_accents_and_case_in_the_query_do_not_change_the_result() -> None:
    chunks = corpus("La revisión de alta demora dos días.", "Otro texto sin relación.")

    assert index(chunks).scores("REVISION") == index(chunks).scores("revisión")
    assert index(chunks).search("REVISION", k=1)[0].chunk_id == "D0#0"


def test_the_index_tokenizes_the_query_with_its_own_tokenizer() -> None:
    chunks = corpus("las entradas pagadas", "otro texto")

    plain = BM25Index(chunks, Tokenizer(stem=False))
    stemmed = BM25Index(chunks, Tokenizer(stem=True))

    assert plain.search("entrada", k=1) == []
    assert [r.chunk_id for r in stemmed.search("entrada", k=1)] == ["D0#0"]


def test_it_indexes_the_index_text_and_not_the_text() -> None:
    headed = Chunk("D0#0", "D0", 0, None, text="hola", index_text="hola historico")

    assert index([headed]).search("historico", k=1)[0].chunk_id == "D0#0"


@pytest.mark.parametrize(
    "parameters",
    [{"k1": -0.1}, {"k1": float("nan")}, {"b": -0.1}, {"b": 1.1}, {"b": float("nan")}],
)
def test_parameters_outside_their_range_are_refused(parameters: dict[str, float]) -> None:
    with pytest.raises(ValueError, match=r"k1|b"):
        index(HAND, **parameters)


@pytest.mark.parametrize("parameters", [{"k1": 0.0}, {"b": 0.0}, {"b": 1.0}])
def test_the_boundary_values_of_the_parameters_are_accepted(parameters: dict[str, float]) -> None:
    assert len(index(HAND, **parameters).scores("casa")) == 3


@pytest.mark.parametrize("k", [0, -1])
def test_searching_for_fewer_than_one_result_is_refused(k: int) -> None:
    with pytest.raises(ValueError, match="k must be"):
        index(HAND).search("casa", k=k)
