"""Evaluation questions and the strict parser of their JSONL file.

`relevant` is a set of documents, not a single answer: a question may need more than one
document, and the metrics are defined over that set (see `metrics`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from merchant_assistant.knowledge.errors import QuestionFormatError

_KEYS = frozenset({"id", "question", "relevant"})


@dataclass(frozen=True, slots=True)
class Question:
    id: str
    text: str
    relevant: frozenset[str]


def parse_questions(raw: str) -> tuple[Question, ...]:
    questions: list[Question] = []
    seen: set[str] = set()
    # `split("\n")` on purpose: `str.splitlines` would cut a line at U+2028, which JSON
    # allows unescaped inside a string.
    for number, line in enumerate(raw.split("\n"), start=1):
        if not line.strip():
            continue
        question = _parse_line(line, number)
        if question.id in seen:
            raise QuestionFormatError(f"line {number}: duplicate question id '{question.id}'")
        seen.add(question.id)
        questions.append(question)
    return tuple(questions)


def _parse_line(line: str, number: int) -> Question:
    try:
        data = json.loads(line)
    except json.JSONDecodeError as error:
        raise QuestionFormatError(f"line {number}: invalid JSON ({error.msg})") from None
    if not isinstance(data, dict):
        raise QuestionFormatError(f"line {number}: expected a JSON object")
    keys = set(data)
    if keys != _KEYS:
        missing = sorted(_KEYS - keys)
        unknown = sorted(keys - _KEYS)
        raise QuestionFormatError(f"line {number}: missing keys {missing}, unknown keys {unknown}")
    question_id = _non_empty_string(data["id"], number, "id")
    text = _non_empty_string(data["question"], number, "question")
    return Question(id=question_id, text=text, relevant=_relevant(data["relevant"], number))


def _non_empty_string(value: object, number: int, key: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise QuestionFormatError(f"line {number}: '{key}' must be a non-empty string")
    return value


def _relevant(value: object, number: int) -> frozenset[str]:
    if not isinstance(value, list) or not value:
        raise QuestionFormatError(f"line {number}: 'relevant' must be a non-empty list")
    ids = [_non_empty_string(item, number, "relevant") for item in value]
    if len(set(ids)) != len(ids):
        raise QuestionFormatError(f"line {number}: 'relevant' repeats a document id")
    return frozenset(ids)
