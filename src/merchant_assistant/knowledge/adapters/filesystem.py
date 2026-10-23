"""The only place of the knowledge capability that reads files.

Keeping the reads here leaves the parsers and the indexes pure: they take text, so the
tests can feed them literals and the corpus can come from anywhere.
"""

from __future__ import annotations

from pathlib import Path

from merchant_assistant.knowledge.documents import Document, parse_document
from merchant_assistant.knowledge.errors import DocumentFormatError, QuestionFormatError
from merchant_assistant.knowledge.questions import Question, parse_questions


def load_documents(directory: Path) -> tuple[Document, ...]:
    paths = sorted(directory.glob("DOC-*.md"))
    if not paths:
        raise DocumentFormatError(f"{directory}: no DOC-*.md files found")
    documents: list[Document] = []
    for path in paths:
        try:
            document = parse_document(path.read_text(encoding="utf-8"))
        except UnicodeDecodeError:
            raise DocumentFormatError(f"{path.name}: not valid UTF-8") from None
        except DocumentFormatError as error:
            raise DocumentFormatError(f"{path.name}: {error.detail}") from error
        if document.id != path.stem:
            # A file that claims another document's id would silently shadow it in citations.
            raise DocumentFormatError(f"{path.name}: front matter id is '{document.id}'")
        documents.append(document)
    return tuple(sorted(documents, key=lambda document: document.id))


def load_questions(path: Path) -> tuple[Question, ...]:
    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise QuestionFormatError(f"{path.name}: not valid UTF-8") from None
    try:
        return parse_questions(raw)
    except QuestionFormatError as error:
        raise QuestionFormatError(f"{path.name}: {error.detail}") from error
