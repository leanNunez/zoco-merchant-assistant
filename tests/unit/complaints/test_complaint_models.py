from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from merchant_assistant.complaints.errors import InvalidComplaint
from merchant_assistant.complaints.models import (
    Complaint,
    ComplaintDraft,
    idempotency_key,
    normalize_text,
)

REASON = "Falta de entrada"
DESCRIPTION = "Me cobraron dos veces la misma entrada"


def draft(reason: str = REASON, description: str = DESCRIPTION) -> ComplaintDraft:
    return ComplaintDraft(reason, description)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("hello", "hello"),
        ("  hello  ", "hello"),
        ("hello   world", "hello world"),
        ("hello\tworld\n\nagain", "hello world again"),
        ("hello\N{NO-BREAK SPACE}world", "hello world"),  # non-breaking space
        ("\N{LATIN SMALL LIGATURE FI}nal", "final"),  # the ligature fi: NFKC splits it
        ("\N{FULLWIDTH LATIN CAPITAL LETTER A}BC", "ABC"),  # fullwidth letters
        (
            "e\N{COMBINING ACUTE ACCENT}",
            "\N{LATIN SMALL LETTER E WITH ACUTE}",
        ),  # e + combining acute -> precomposed e with acute
        ("", ""),
        ("   ", ""),
    ],
)
def test_normalization_trims_collapses_whitespace_and_applies_nfkc(raw: str, expected: str) -> None:
    assert normalize_text(raw) == expected


def test_normalization_keeps_the_casing() -> None:
    assert normalize_text("Falta DE entrada") == "Falta DE entrada"


def test_the_key_of_a_known_complaint_is_pinned() -> None:
    # printf 'conv-1\nfalta de entrada\nme cobraron dos veces la misma entrada' | sha256sum
    assert idempotency_key("conv-1", draft()) == (
        "0c2d18a26c87871945d82b4a1779c917c4b7f35e435530cbe8af23479672274c"
    )


@pytest.mark.parametrize(
    ("reason", "description"),
    [
        ("FALTA DE ENTRADA", "ME COBRARON DOS VECES LA MISMA ENTRADA"),
        ("falta de entrada", "me cobraron dos veces la misma entrada"),
        ("  Falta   de\tentrada ", "Me cobraron\n dos veces la misma   entrada  "),
        ("Falta de entrada", "Me cobraron dos veces la misma entrada\N{NO-BREAK SPACE}"),
        ("\N{FULLWIDTH LATIN CAPITAL LETTER F}alta de entrada", DESCRIPTION),  # fullwidth F
        (
            "Falta de entrada",
            "Me cobraron dos veces la misma entrada".replace(
                "\N{LATIN SMALL LETTER E WITH ACUTE}", "e\N{COMBINING ACUTE ACCENT}"
            ),
        ),
    ],
)
def test_the_same_complaint_typed_differently_gets_the_same_key(
    reason: str, description: str
) -> None:
    assert idempotency_key("conv-1", draft(reason, description)) == idempotency_key(
        "conv-1", draft()
    )


def test_an_accent_typed_as_two_code_points_equals_the_precomposed_one() -> None:
    assert idempotency_key("c", draft("Cobro indebido", "Devolucio\u0301n no acreditada")) == (
        idempotency_key("c", draft("Cobro indebido", "Devoluci\u00f3n no acreditada"))
    )


def test_casefolding_equates_the_german_sharp_s() -> None:
    assert idempotency_key(
        "c", draft("Gro\N{LATIN SMALL LETTER SHARP S}e Falla", DESCRIPTION)
    ) == idempotency_key("c", draft("GROSSE FALLA", DESCRIPTION))


@pytest.mark.parametrize(
    ("conversation", "reason", "description"),
    [
        ("conv-2", REASON, DESCRIPTION),
        ("CONV-1", REASON, DESCRIPTION),  # the conversation id is an identifier, not text
        ("conv-1", "Entrada duplicada", DESCRIPTION),
        ("conv-1", REASON, "Me cobraron tres veces la misma entrada"),
        ("conv-1", REASON, "Me cobraron dos veces la misma entrada."),
    ],
)
def test_another_conversation_or_other_text_gets_another_key(
    conversation: str, reason: str, description: str
) -> None:
    assert idempotency_key(conversation, draft(reason, description)) != idempotency_key(
        "conv-1", draft()
    )


def test_moving_words_between_reason_and_description_changes_the_key() -> None:
    one = idempotency_key("c", draft("aaa bbb", "ccc ddd eee"))
    other = idempotency_key("c", draft("aaa", "bbb ccc ddd eee"))

    assert one != other


def test_a_blank_conversation_has_no_key() -> None:
    with pytest.raises(ValueError, match="conversation_id is blank"):
        idempotency_key("  ", draft())


def test_the_key_is_a_sha256_hex_digest() -> None:
    key = idempotency_key("conv-1", draft())

    assert len(key) == 64
    assert set(key) <= set("0123456789abcdef")


def test_the_draft_stores_the_normalized_text_with_the_users_casing() -> None:
    stored = draft("  Falta   DE entrada ", " Me cobraron\n dos veces la misma ENTRADA")

    assert (stored.reason, stored.description) == (
        "Falta DE entrada",
        "Me cobraron dos veces la misma ENTRADA",
    )


@pytest.mark.parametrize("reason", ["abc", "a" * 80, "  abc  ", "a b"])
def test_a_reason_of_three_to_eighty_characters_is_valid(reason: str) -> None:
    assert draft(reason).reason == normalize_text(reason)


@pytest.mark.parametrize("reason", ["", "ab", "   ", " a ", "a" * 81])
def test_a_reason_outside_three_to_eighty_characters_is_refused(reason: str) -> None:
    with pytest.raises(InvalidComplaint, match="reason has"):
        draft(reason)


@pytest.mark.parametrize("description", ["a" * 10, "a" * 1000, "  " + "a" * 10 + "  "])
def test_a_description_of_ten_to_a_thousand_characters_is_valid(description: str) -> None:
    assert draft(description=description).description == description.strip()


@pytest.mark.parametrize("description", ["", "a" * 9, "   ", "a" * 1001, "ab cd e f"])
def test_a_description_outside_ten_to_a_thousand_characters_is_refused(description: str) -> None:
    with pytest.raises(InvalidComplaint, match="description has"):
        draft(description=description)


def test_the_limits_are_measured_after_normalization() -> None:
    padded = "a" + " " * 500 + "b" * 9  # 510 raw characters, 11 once the spaces collapse

    assert draft(description=padded).description == "a " + "b" * 9


def test_the_refusal_has_a_stable_code_and_a_message_with_the_limits() -> None:
    with pytest.raises(InvalidComplaint) as refused:
        draft("ab")

    assert refused.value.detail == "reason has 2 characters, expected 3 to 80"
    assert refused.value.public() == {
        "code": "invalid_complaint",
        "message": "The reason must have 3 to 80 characters and the description 10 to 1000.",
    }


def test_a_draft_cannot_be_modified_after_it_is_confirmed() -> None:
    confirmed = draft()

    with pytest.raises(FrozenInstanceError):
        confirmed.reason = "otra cosa"  # type: ignore[misc]


def test_a_complaint_needs_an_aware_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        Complaint("REC-000001", draft(), "conv-1", "k", datetime(2026, 10, 23, 12, 0))  # noqa: DTZ001

    aware = Complaint(
        "REC-000001", draft(), "conv-1", "k", datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
    )
    assert aware.created_at.utcoffset() is not None
