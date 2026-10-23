from datetime import date

import pytest

from merchant_assistant.knowledge.chunking import Chunk, ChunkingStrategy, chunk_corpus
from merchant_assistant.knowledge.documents import Document, Section


def make_document(
    doc_id: str = "DOC-01", sections: tuple[Section, ...] = (Section("Uno", "a b"),)
) -> Document:
    return Document(
        id=doc_id,
        title="Guía",
        kind="guia",
        valid_from=date(2026, 1, 1),
        valid_until=date(2026, 12, 31),
        sections=sections,
    )


# Headings count as words once flattened: two headings plus eight body words make ten.
TEN_WORDS = make_document(sections=(Section("w0", "w1 w2 w3 w4"), Section("w5", "w6 w7 w8 w9")))


def test_the_ten_word_fixture_flattens_to_ten_words() -> None:
    assert TEN_WORDS.text.split() == [f"w{i}" for i in range(10)]


def test_a_window_of_four_words_with_overlap_one_covers_ten_words_in_three_windows() -> None:
    chunks = ChunkingStrategy("window", size=4, overlap=1).apply(TEN_WORDS)

    assert [chunk.text for chunk in chunks] == [
        "w0 w1 w2 w3",  # words[0:4]
        "w3 w4 w5 w6",  # words[3:7]: starts one word before the previous end
        "w6 w7 w8 w9",  # words[6:10]: reaches the end, so there is no redundant tail
    ]


def test_windows_without_overlap_tile_the_words_and_keep_a_short_last_window() -> None:
    chunks = ChunkingStrategy("window", size=4).apply(TEN_WORDS)

    assert [chunk.text for chunk in chunks] == [
        "w0 w1 w2 w3",
        "w4 w5 w6 w7",
        "w8 w9",
    ]


def test_a_window_that_ends_exactly_at_the_last_word_adds_no_empty_tail() -> None:
    chunks = ChunkingStrategy("window", size=5).apply(TEN_WORDS)

    assert [chunk.text for chunk in chunks] == ["w0 w1 w2 w3 w4", "w5 w6 w7 w8 w9"]


def test_a_document_with_fewer_words_than_the_window_yields_a_single_chunk() -> None:
    chunks = ChunkingStrategy("window", size=64, overlap=10).apply(TEN_WORDS)

    assert [chunk.text for chunk in chunks] == [" ".join(f"w{i}" for i in range(10))]


def test_a_window_of_one_word_without_overlap_yields_one_chunk_per_word() -> None:
    chunks = ChunkingStrategy("window", size=1).apply(TEN_WORDS)

    assert [chunk.text for chunk in chunks] == [f"w{i}" for i in range(10)]


def test_words_are_joined_with_a_single_space_whatever_the_original_spacing() -> None:
    document = make_document(sections=(Section("A", "uno\n\n  dos\ttres"),))

    chunks = ChunkingStrategy("window", size=10).apply(document)

    assert chunks[0].text == "A uno dos tres"


def test_window_chunks_have_no_heading_and_zero_based_ids() -> None:
    chunks = ChunkingStrategy("window", size=4, overlap=1).apply(TEN_WORDS)

    assert [(chunk.id, chunk.doc_id, chunk.ordinal, chunk.heading) for chunk in chunks] == [
        ("DOC-01#0", "DOC-01", 0, None),
        ("DOC-01#1", "DOC-01", 1, None),
        ("DOC-01#2", "DOC-01", 2, None),
    ]


def test_the_document_strategy_makes_one_chunk_with_the_whole_text() -> None:
    chunks = ChunkingStrategy("document").apply(TEN_WORDS)

    assert chunks == (
        Chunk(
            id="DOC-01#0",
            doc_id="DOC-01",
            ordinal=0,
            heading=None,
            text="w0\nw1 w2 w3 w4\n\nw5\nw6 w7 w8 w9",
            index_text="w0\nw1 w2 w3 w4\n\nw5\nw6 w7 w8 w9",
        ),
    )


def test_the_section_strategy_makes_one_chunk_per_section_with_its_heading() -> None:
    document = make_document(
        sections=(Section("Alcance", "Texto uno."), Section("Detalle", "Dos."))
    )

    chunks = ChunkingStrategy("section").apply(document)

    assert chunks == (
        Chunk("DOC-01#0", "DOC-01", 0, "Alcance", "Alcance\nTexto uno.", "Alcance\nTexto uno."),
        Chunk("DOC-01#1", "DOC-01", 1, "Detalle", "Detalle\nDos.", "Detalle\nDos."),
    )


def test_a_preamble_section_becomes_a_chunk_without_heading() -> None:
    document = make_document(sections=(Section("", "Preámbulo."), Section("Alcance", "Texto.")))

    chunks = ChunkingStrategy("section").apply(document)

    assert [(chunk.heading, chunk.text) for chunk in chunks] == [
        (None, "Preámbulo."),
        ("Alcance", "Alcance\nTexto."),
    ]


def test_the_header_changes_only_the_index_text() -> None:
    plain = ChunkingStrategy("section").apply(make_document())
    headed = ChunkingStrategy("section", header=True).apply(make_document())

    assert headed[0].text == plain[0].text == "Uno\na b"
    assert headed[0].index_text == "Guía | guia | vigente 2026-01-01 a 2026-12-31\nUno\na b"
    assert plain[0].index_text == "Uno\na b"


@pytest.mark.parametrize("kind", ["document", "section"])
def test_the_header_applies_to_every_strategy(kind: str) -> None:
    strategy = ChunkingStrategy(kind, header=True)  # type: ignore[arg-type]

    chunk = strategy.apply(make_document())[0]

    assert chunk.index_text.startswith("Guía | guia | vigente 2026-01-01 a 2026-12-31\n")
    assert not chunk.text.startswith("Guía")


def test_a_window_with_header_keeps_the_text_free_of_the_header() -> None:
    chunk = ChunkingStrategy("window", size=3, header=True).apply(TEN_WORDS)[0]

    assert chunk.text == "w0 w1 w2"
    assert chunk.index_text == "Guía | guia | vigente 2026-01-01 a 2026-12-31\nw0 w1 w2"


def test_the_corpus_is_chunked_in_document_order_with_ids_per_document() -> None:
    second = make_document("DOC-02", (Section("Dos", "x"), Section("Tres", "y")))
    first = make_document("DOC-01", (Section("Uno", "z"),))

    chunks = chunk_corpus([second, first], ChunkingStrategy("section"))

    assert [chunk.id for chunk in chunks] == ["DOC-02#0", "DOC-02#1", "DOC-01#0"]


def test_an_empty_corpus_has_no_chunks() -> None:
    assert chunk_corpus([], ChunkingStrategy("document")) == ()


@pytest.mark.parametrize(
    ("strategy", "label"),
    [
        (ChunkingStrategy("document"), "document"),
        (ChunkingStrategy("section"), "section"),
        (ChunkingStrategy("section", header=True), "section+header"),
        (ChunkingStrategy("window", size=64, overlap=10), "window-64-o10"),
        (ChunkingStrategy("window", size=32), "window-32-o0"),
        (ChunkingStrategy("window", size=64, overlap=10, header=True), "window-64-o10+header"),
    ],
)
def test_the_label_names_the_strategy_and_its_parameters(
    strategy: ChunkingStrategy, label: str
) -> None:
    assert strategy.label == label


@pytest.mark.parametrize(
    "kwargs",
    [
        {"kind": "window"},
        {"kind": "window", "size": 0},
        {"kind": "window", "size": -3},
        {"kind": "window", "size": 4, "overlap": 4},
        {"kind": "window", "size": 4, "overlap": 9},
        {"kind": "window", "size": 4, "overlap": -1},
        {"kind": "document", "size": 4},
        {"kind": "document", "overlap": 1},
        {"kind": "section", "size": 4},
        {"kind": "section", "overlap": 1},
    ],
)
def test_a_strategy_with_impossible_parameters_is_refused(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError, match=r"size|overlap"):
        ChunkingStrategy(**kwargs)  # type: ignore[arg-type]
