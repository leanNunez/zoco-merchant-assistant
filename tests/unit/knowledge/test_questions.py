import pytest

from merchant_assistant.knowledge.errors import QuestionFormatError
from merchant_assistant.knowledge.questions import Question, parse_questions
from merchant_assistant.shared.errors import DomainError

LS = chr(0x2028)  # LINE SEPARATOR: `str.splitlines` would cut here, JSON and Markdown do not
LINE_1 = '{"id": "Q01", "question": "¿Qué rol publica?", "relevant": ["DOC-01", "DOC-02"]}'
LINE_2 = '{"id": "Q02", "question": "¿Cuánto demora?", "relevant": ["DOC-02"]}'


def test_valid_lines_become_questions_with_a_set_of_relevant_documents() -> None:
    questions = parse_questions(LINE_1 + "\n" + LINE_2 + "\n")

    assert questions == (
        Question("Q01", "¿Qué rol publica?", frozenset({"DOC-01", "DOC-02"})),
        Question("Q02", "¿Cuánto demora?", frozenset({"DOC-02"})),
    )


def test_blank_lines_are_skipped_without_shifting_the_reported_line_numbers() -> None:
    raw = "\n" + LINE_1 + "\n   \n\n" + "{broken"

    with pytest.raises(QuestionFormatError) as refused:
        parse_questions(raw)

    assert refused.value.detail.startswith("line 5:")


def test_an_empty_file_has_no_questions() -> None:
    assert parse_questions("") == ()
    assert parse_questions("\n\n") == ()


def test_a_unicode_line_separator_inside_a_question_does_not_split_the_line() -> None:
    raw = f'{{"id": "Q01", "question": "uno{LS}dos", "relevant": ["DOC-01"]}}'

    assert parse_questions(raw)[0].text == f"uno{LS}dos"


@pytest.mark.parametrize(
    ("line", "fragment"),
    [
        ("{not json", "invalid JSON"),
        ('["Q01"]', "expected a JSON object"),
        ('"Q01"', "expected a JSON object"),
        ('{"id": "Q01", "relevant": ["DOC-01"]}', "missing keys ['question']"),
        (
            '{"id": "Q01", "question": "x", "relevant": ["DOC-01"], "extra": 1}',
            "unknown keys ['extra']",
        ),
        ('{"id": "", "question": "x", "relevant": ["DOC-01"]}', "'id' must be a non-empty string"),
        ('{"id": 7, "question": "x", "relevant": ["DOC-01"]}', "'id' must be a non-empty string"),
        ('{"id": "Q01", "question": "  ", "relevant": ["DOC-01"]}', "'question' must be"),
        ('{"id": "Q01", "question": "x", "relevant": []}', "'relevant' must be a non-empty list"),
        (
            '{"id": "Q01", "question": "x", "relevant": "DOC-01"}',
            "'relevant' must be a non-empty list",
        ),
        (
            '{"id": "Q01", "question": "x", "relevant": ["DOC-01", ""]}',
            "'relevant' must be a non-empty string",
        ),
        (
            '{"id": "Q01", "question": "x", "relevant": ["DOC-01", 3]}',
            "'relevant' must be a non-empty string",
        ),
        (
            '{"id": "Q01", "question": "x", "relevant": ["DOC-01", "DOC-01"]}',
            "repeats a document id",
        ),
    ],
)
def test_a_line_that_breaks_the_format_is_refused_with_its_line_number(
    line: str, fragment: str
) -> None:
    with pytest.raises(QuestionFormatError) as refused:
        parse_questions(LINE_1 + "\n" + line)

    assert refused.value.detail.startswith("line 2:")
    assert fragment in refused.value.detail


def test_a_repeated_question_id_is_refused_at_the_second_occurrence() -> None:
    with pytest.raises(QuestionFormatError) as refused:
        parse_questions(LINE_1 + "\n" + LINE_2 + "\n" + LINE_1)

    assert refused.value.detail == "line 3: duplicate question id 'Q01'"


def test_the_error_has_a_stable_code_and_a_message_that_hides_the_detail() -> None:
    with pytest.raises(QuestionFormatError) as refused:
        parse_questions("{broken")

    assert isinstance(refused.value, DomainError)
    assert refused.value.public() == {
        "code": "invalid_question",
        "message": "A question could not be read.",
    }
