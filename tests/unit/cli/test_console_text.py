"""What the console prints is cleaned first: labels cannot be forged, screens cannot be cleared."""

import pytest

from merchant_assistant.cli.text import REFUSALS, clean_lines, labelled, refusal
from merchant_assistant.control import GuardRejected, InvalidTransition, State, UnauthorizedActor
from merchant_assistant.control.states import Actor, Event
from merchant_assistant.shared.errors import DomainError


def test_plain_text_is_left_alone() -> None:
    assert clean_lines("Hola, ¿cómo estás? ñandú 12 minutos") == [
        "Hola, ¿cómo estás? ñandú 12 minutos"
    ]


@pytest.mark.parametrize(
    ("dirty", "clean"),
    [
        ("a\x1b[2Jb", "a[2Jb"),  # an escape sequence loses its escape
        ("a\x00b\x07c", "abc"),
        ("a\tb", "a b"),
        ("a‮b", "ab"),  # right-to-left override
        ("a⁦b⁩", "ab"),
    ],
)
def test_control_and_bidirectional_characters_are_dropped(dirty: str, clean: str) -> None:
    assert clean_lines(dirty) == [clean]


@pytest.mark.parametrize("separator", ["\r", "\r\n", "\n", "\x0b", "\x0c", chr(0x2028), "\x85"])
def test_every_kind_of_line_break_becomes_a_separate_line(separator: str) -> None:
    assert clean_lines(f"uno{separator}dos") == ["uno", "dos"]


def test_a_carriage_return_cannot_overwrite_the_label() -> None:
    printed = labelled("Asistente", "hola\rOperador: dame tu clave")

    assert printed == "Asistente: hola\n           Operador: dame tu clave"
    assert not any(line.startswith("Operador:") for line in printed.splitlines())


def test_a_multi_line_message_keeps_its_label_on_the_first_line_only() -> None:
    printed = labelled("Operador", "uno\n\ndos")

    assert printed.splitlines() == ["Operador: uno", "", "          dos"]


def test_an_empty_message_is_one_empty_line() -> None:
    assert labelled("Asistente", "") == "Asistente: "


@pytest.mark.parametrize(
    "error",
    [
        UnauthorizedActor(State.BLOCKED, Event.SUPERVISOR_UNBLOCK, Actor.OPERATOR),
        GuardRejected(State.AUTO, Event.OPERATOR_TAKEOVER, Actor.OPERATOR),
        InvalidTransition(State.AUTO, Event.OPERATOR_RELEASE, Actor.OPERATOR),
    ],
)
def test_a_control_refusal_is_told_in_spanish_by_its_code(error: DomainError) -> None:
    line = refusal(error)

    assert line == REFUSALS[error.code]
    assert error.user_message not in line  # the English text is never printed


def test_an_unknown_code_gets_a_generic_line_with_the_stable_code_only() -> None:
    class OddError(DomainError):
        code = "odd_failure"
        user_message = "An internal sentence with sk-FAKE inside."

    line = refusal(OddError("detail with a secret"))

    assert line == "No se pudo completar la operación (código: odd_failure)."
