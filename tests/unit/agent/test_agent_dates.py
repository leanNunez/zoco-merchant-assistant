"""Grounded dates: a sales date must be one the user stated, or one they accepted."""

from datetime import date

import pytest

from merchant_assistant.agent.domain.dates import (
    accepted_assistant_dates,
    dates_stated_by_assistant,
    explicit_dates,
    is_grounded,
)
from merchant_assistant.agent.domain.messages import Message, Role, ToolCall

OCT_22 = date(2026, 10, 22)


def user(text: str) -> Message:
    return Message(Role.USER, text)


def assistant(text: str) -> Message:
    return Message(Role.ASSISTANT, text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("¿cómo vendí el 2026-10-22?", {OCT_22}, id="iso"),
        pytest.param("ventas del 22/10/2026", {OCT_22}, id="day above 12 with slashes"),
        pytest.param("ventas del 22-10-2026", {OCT_22}, id="day above 12 with dashes"),
        pytest.param("13/04/2026", {date(2026, 4, 13)}, id="13/04/2026 is unambiguous"),
        pytest.param("04/04/2026", {date(2026, 4, 4)}, id="day equals month"),
        pytest.param("22 de octubre de 2026", {OCT_22}, id="long form"),
        pytest.param("22 octubre 2026", {OCT_22}, id="long form without de"),
        pytest.param("22 de octubre del 2026", {OCT_22}, id="long form with del"),
        pytest.param("octubre 22, 2026", {OCT_22}, id="month first"),
        pytest.param("22 DE OCTUBRE DE 2026", {OCT_22}, id="upper case"),
        pytest.param("5 de setiembre de 2026", {date(2026, 9, 5)}, id="setiembre"),
        pytest.param("5 de septiembre de 2026", {date(2026, 9, 5)}, id="septiembre"),
        pytest.param("el viernes 2 de octubre de 2026", {date(2026, 10, 2)}, id="with a weekday"),
        pytest.param(
            "\uff12\uff10\uff12\uff16-\uff11\uff10-\uff12\uff12", {OCT_22}, id="full-width digits"
        ),
        pytest.param(
            "el 2026-10-01 y el 2026-10-02",
            {date(2026, 10, 1), date(2026, 10, 2)},
            id="two dates",
        ),
        pytest.param("03/04", set(), id="03/04 without a year"),
        pytest.param("3/4/2026", set(), id="3/4/2026 is ambiguous"),
        pytest.param("03/04/2026", set(), id="03/04/2026 is ambiguous"),
        pytest.param("04/13/2026", set(), id="month first numeric is not read"),
        pytest.param("22/10/26", set(), id="two-digit year"),
        pytest.param("ayer", set(), id="ayer"),
        pytest.param("hoy", set(), id="hoy"),
        pytest.param("el viernes pasado", set(), id="el viernes pasado"),
        pytest.param("la semana pasada", set(), id="la semana pasada"),
        pytest.param("22 de octubre", set(), id="long form without a year"),
        pytest.param("31/02/2026", set(), id="31/02/2026 is not a date"),
        pytest.param("2026-02-30", set(), id="2026-02-30 is not a date"),
        pytest.param("ticket 1222/10/20261", set(), id="inside a longer number"),
        pytest.param("32 de octubre de 2026", set(), id="day out of range in long form"),
    ],
)
def test_only_unambiguous_explicit_dates_are_extracted(text: str, expected: set[date]) -> None:
    assert explicit_dates(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("¿Te referís al 22/10/2026?", {OCT_22}, id="day first slashes"),
        pytest.param("¿Te referís al 03/04/2026?", {date(2026, 4, 3)}, id="read day first"),
        pytest.param("¿Es el 2026-10-22?", {OCT_22}, id="iso"),
        pytest.param("¿Es el 22 de octubre de 2026?", {OCT_22}, id="long form"),
        pytest.param("¿Ayer?", set(), id="relative"),
    ],
)
def test_an_assistant_states_dates_in_its_own_day_first_order(
    text: str, expected: set[date]
) -> None:
    assert dates_stated_by_assistant(text) == expected


def test_a_date_the_user_wrote_explicitly_is_grounded() -> None:
    history = [user("¿cuánto vendí el 22 de octubre de 2026?")]

    assert is_grounded(OCT_22, history)


def test_a_date_written_in_an_earlier_user_message_is_still_grounded() -> None:
    history = [user("hablemos del 22/10/2026"), assistant("Claro."), user("¿y las ventas?")]

    assert is_grounded(OCT_22, history)


@pytest.mark.parametrize("text", ["ayer", "el viernes pasado", "03/04", "3/4/2026"])
def test_a_date_the_model_inferred_from_a_relative_or_ambiguous_one_is_not_grounded(
    text: str,
) -> None:
    assert not is_grounded(OCT_22, [user(text)])
    assert not is_grounded(date(2026, 4, 3), [user(text)])


def test_a_date_the_assistant_proposed_and_the_user_accepted_is_grounded() -> None:
    history = [user("¿cuánto vendí ayer?"), assistant("¿Te referís al 22/10/2026?"), user("sí")]

    assert is_grounded(OCT_22, history)
    assert accepted_assistant_dates(history) == {OCT_22}


def test_only_the_date_the_assistant_proposed_is_accepted() -> None:
    history = [user("¿cuánto vendí ayer?"), assistant("¿Te referís al 22/10/2026?"), user("sí")]

    assert not is_grounded(date(2026, 10, 21), history)


@pytest.mark.parametrize(
    "reply",
    ["no", "sí pero el 21", "¿sí?", "creo que sí, o el 21", "sí\ny también el 21"],
)
def test_anything_but_a_strict_yes_does_not_accept_the_proposed_date(reply: str) -> None:
    history = [user("¿cuánto vendí ayer?"), assistant("¿Te referís al 22/10/2026?"), user(reply)]

    assert not is_grounded(OCT_22, history)


def test_a_yes_accepts_only_the_message_right_before_it() -> None:
    history = [
        user("¿cuánto vendí ayer?"),
        assistant("¿Te referís al 22/10/2026?"),
        user("esperá"),
        assistant("Dale, decime."),
        user("sí"),
    ]

    assert not is_grounded(OCT_22, history)


def test_a_date_only_inside_a_tool_result_is_not_grounded() -> None:
    call = ToolCall("c1", "buscar_documentacion", '{"consulta":"x"}')
    history = [
        user("¿qué día cerró la liquidación?"),
        Message(Role.ASSISTANT, None, (call,)),
        Message(Role.TOOL, '{"data": "2026-10-22"}', tool_call_id="c1"),
        user("sí"),
    ]

    assert not is_grounded(OCT_22, history)


def test_a_date_the_assistant_stated_without_a_reply_is_not_grounded() -> None:
    history = [user("¿cuánto vendí ayer?"), assistant("Vendiste mucho el 22/10/2026.")]

    assert not is_grounded(OCT_22, history)
