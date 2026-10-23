from datetime import date
from pathlib import Path

import pytest

from merchant_assistant.knowledge.adapters.filesystem import load_documents, load_questions

pytestmark = pytest.mark.integration

# Resolved from this file, not from the working directory, so the test runs from anywhere.
REPOSITORY = Path(__file__).resolve().parents[3]


def test_the_kit_has_twelve_documents_in_id_order() -> None:
    documents = load_documents(REPOSITORY / "docs")

    assert [document.id for document in documents] == [f"DOC-{n:02d}" for n in range(1, 13)]


def test_the_kit_has_twenty_questions() -> None:
    questions = load_questions(REPOSITORY / "questions.jsonl")

    assert [question.id for question in questions] == [f"Q{n:02d}" for n in range(1, 21)]


def test_every_document_has_at_least_three_sections() -> None:
    documents = load_documents(REPOSITORY / "docs")

    assert {
        document.id: len(document.sections) for document in documents if len(document.sections) < 3
    } == {}


def test_every_relevant_document_of_every_question_exists_in_the_corpus() -> None:
    corpus = {document.id for document in load_documents(REPOSITORY / "docs")}
    questions = load_questions(REPOSITORY / "questions.jsonl")

    unknown = {
        question.id: question.relevant - corpus
        for question in questions
        if question.relevant - corpus
    }

    assert unknown == {}


def test_only_the_first_and_last_questions_have_two_relevant_documents() -> None:
    questions = {q.id: q for q in load_questions(REPOSITORY / "questions.jsonl")}

    assert questions["Q01"].relevant == {"DOC-01", "DOC-02"}
    assert questions["Q20"].relevant == {"DOC-02", "DOC-03"}
    assert [q.id for q in questions.values() if len(q.relevant) != 1] == ["Q01", "Q20"]


def test_the_historical_document_ends_the_day_before_the_current_tariffs_start() -> None:
    documents = {d.id: d for d in load_documents(REPOSITORY / "docs")}

    assert documents["DOC-12"].valid_until == date(2026, 8, 31)
    assert documents["DOC-05"].valid_from == date(2026, 9, 1)
    assert documents["DOC-07"].valid_from == date(2026, 9, 1)


def test_the_kit_keeps_the_accents_of_the_original_text() -> None:
    documents = {d.id: d for d in load_documents(REPOSITORY / "docs")}

    assert "Distinción de productos" in [s.heading for s in documents["DOC-01"].sections]
