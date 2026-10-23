from pathlib import Path

import pytest

from merchant_assistant.knowledge.adapters.filesystem import load_documents, load_questions
from merchant_assistant.knowledge.errors import DocumentFormatError, QuestionFormatError

pytestmark = pytest.mark.integration


def document_text(doc_id: str) -> str:
    return (
        f"---\nid: {doc_id}\ntitulo: Prueba\ntipo: guia\n"
        "vigencia_desde: 2026-01-01\nvigencia_hasta: 2026-12-31\n---\n\n# Uno\nTexto.\n"
    )


def test_documents_are_loaded_sorted_by_id_and_other_files_are_ignored(tmp_path: Path) -> None:
    (tmp_path / "DOC-10.md").write_text(document_text("DOC-10"), encoding="utf-8")
    (tmp_path / "DOC-02.md").write_text(document_text("DOC-02"), encoding="utf-8")
    (tmp_path / "NOTES.md").write_text("no front matter, not a document", encoding="utf-8")

    documents = load_documents(tmp_path)

    assert [document.id for document in documents] == ["DOC-02", "DOC-10"]


def test_a_file_whose_name_disagrees_with_its_id_is_refused(tmp_path: Path) -> None:
    (tmp_path / "DOC-02.md").write_text(document_text("DOC-03"), encoding="utf-8")

    with pytest.raises(DocumentFormatError) as refused:
        load_documents(tmp_path)

    assert refused.value.detail == "DOC-02.md: front matter id is 'DOC-03'"


def test_a_parse_error_names_the_file_that_caused_it(tmp_path: Path) -> None:
    (tmp_path / "DOC-01.md").write_text(document_text("DOC-01"), encoding="utf-8")
    (tmp_path / "DOC-02.md").write_text("sin front matter", encoding="utf-8")

    with pytest.raises(DocumentFormatError) as refused:
        load_documents(tmp_path)

    assert refused.value.detail.startswith("DOC-02.md: line 1:")


def test_a_directory_without_documents_is_refused_instead_of_loading_an_empty_corpus(
    tmp_path: Path,
) -> None:
    with pytest.raises(DocumentFormatError):
        load_documents(tmp_path)


def test_a_document_that_is_not_utf8_is_refused(tmp_path: Path) -> None:
    (tmp_path / "DOC-01.md").write_bytes(b"---\nid: DOC-01\n\xff\xfe")

    with pytest.raises(DocumentFormatError) as refused:
        load_documents(tmp_path)

    assert refused.value.detail == "DOC-01.md: not valid UTF-8"


def test_questions_are_loaded_from_a_utf8_file(tmp_path: Path) -> None:
    path = tmp_path / "q.jsonl"
    path.write_text(
        '{"id": "Q01", "question": "¿Año?", "relevant": ["DOC-01"]}\n', encoding="utf-8"
    )

    questions = load_questions(path)

    assert [(q.id, q.text) for q in questions] == [("Q01", "¿Año?")]


def test_a_question_error_names_the_file_and_the_line(tmp_path: Path) -> None:
    path = tmp_path / "q.jsonl"
    path.write_text("\n{broken\n", encoding="utf-8")

    with pytest.raises(QuestionFormatError) as refused:
        load_questions(path)

    assert refused.value.detail.startswith("q.jsonl: line 2:")


def test_a_questions_file_that_is_not_utf8_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "q.jsonl"
    path.write_bytes(b"\xff\xfe")

    with pytest.raises(QuestionFormatError):
        load_questions(path)
