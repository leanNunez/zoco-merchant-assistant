import ast
import hashlib
import hmac
import re
import time
from pathlib import Path

import pytest

from cortex.bootstrap import DEFAULT_TRACE_KEY
from cortex.observability.redaction import (
    ALLOWED_ATTRIBUTES,
    REDACTED,
    TraceRedactor,
    scrub,
    scrub_tree,
)

SOURCE = Path(__file__).resolve().parents[3] / "src" / "cortex"


@pytest.mark.parametrize(
    ("secret", "leftover"),
    [
        ("mi tarjeta es 4111 1111 1111 1111 gracias", "4111"),
        ("tarjeta4111111111111111", "4111111111111111"),  # glued to letters
        ("escribime a juana.perez@example.com", "juana"),
        ("la clave sk-live-AbCdEf1234567890XyZq", "AbCdEf"),
        ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc", "eyJhbGci"),
        ("llamame al +54 9 11 5555-1234", "5555-1234"),
        ("CUIT 20-12345678-9", "12345678"),
        ("DNI 30123456", "30123456"),
        ("token 3f2a9c4e8b1d7a6f5e0c9b8a7d6e5f4a3b2c1d0e", "3f2a9c4e"),
    ],
)
def test_secrets_and_personal_numbers_are_masked(secret: str, leftover: str) -> None:
    cleaned = scrub(secret)

    assert leftover not in cleaned
    assert REDACTED in cleaned


@pytest.mark.parametrize(
    "harmless", ["op_claim_001", "consultar_ventas", "AUTO", "tool_unavailable", "OPTED_OUT,CLOSED"]
)
def test_the_names_and_codes_the_traces_are_made_of_survive(harmless: str) -> None:
    assert scrub(harmless) == harmless


# Adversarial shapes for a backtracking engine. 200 KB each: a quadratic pattern needs tens of
# seconds on any of them, a linear one a few milliseconds, so the 2 s bound is far from both.
_HOSTILE_SIZE = 200_000
_HOSTILE_INPUTS = {
    "one long word": "a" * _HOSTILE_SIZE,
    "a long run of digits": "1" * _HOSTILE_SIZE,
    "a@ repeated": "a@" * (_HOSTILE_SIZE // 2),
    "a domain that never ends": "a@b" + ".b" * (_HOSTILE_SIZE // 2),
    "dot, plus and dash runs": ".+-" * (_HOSTILE_SIZE // 3),
    "Bearer repeated": "Bearer " * (_HOSTILE_SIZE // 7),
    "Bearer then spaces": "Bearer" + " " * _HOSTILE_SIZE,
    "dashes between digits": "1" + "-" * _HOSTILE_SIZE + "1",
    "digits joined by dashes": "1-" * (_HOSTILE_SIZE // 2) + "x",
    "digits joined by spaces, then a dash": "1 " * (_HOSTILE_SIZE // 2) + "-",
    "digits joined by dots, then a letter": "1." * (_HOSTILE_SIZE // 2) + "x",
    "key prefix repeated": "sk-" * (_HOSTILE_SIZE // 3),
    "a token that ends in a non-ASCII letter": "a" * _HOSTILE_SIZE + "\u00e9",
}


@pytest.mark.parametrize("shape", list(_HOSTILE_INPUTS))
def test_masking_is_linear_on_hostile_input(shape: str) -> None:
    text = _HOSTILE_INPUTS[shape]

    started = time.perf_counter()
    scrub(text)
    scrub(text, limit=len(text))

    assert time.perf_counter() - started < 2.0


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # A chain of digits whose end is glued to a dash or a letter cannot be masked from its
        # start, but the part that ends at a space still is.
        ("1 2 3 4 5 6 7 8-", f"{REDACTED} 8-"),
        ("a4111 1111 1111 1111", f"a4111 {REDACTED}"),
        ("tel 11 5555 1234 fin", f"tel {REDACTED} fin"),
        ("1-2-3-4-5-6-7-x", "1-2-3-4-5-6-7-x"),
        ("pedido 12-34-56", "pedido 12-34-56"),
    ],
)
def test_digit_chains_keep_their_masking_boundaries(text: str, expected: str) -> None:
    assert scrub(text) == expected


def test_a_value_is_cut_to_its_limit() -> None:
    sentence = "una frase normal " * 40
    assert len(scrub(sentence)) == 120
    assert len(scrub(sentence, limit=200)) == 200


def test_masking_reaches_into_nested_values() -> None:
    cleaned = scrub_tree({"a": ["mail x@y.com", {"b": "ok"}], "n": 3})

    assert cleaned == {"a": [f"mail {REDACTED}", {"b": "ok"}], "n": 3}


def test_only_allowlisted_attributes_are_kept_and_the_rest_is_counted() -> None:
    redactor = TraceRedactor("key")

    kept, dropped = redactor.clean(
        "tool_requested", {"tool": "consultar_ventas", "arguments": "fecha=2026", "x": 1}
    )

    assert kept == {"tool": "consultar_ventas"}
    assert dropped == 2


def test_an_event_type_nobody_allowed_carries_nothing() -> None:
    assert TraceRedactor("key").clean("brand_new_event", {"anything": "at all"}) == ({}, 1)


def test_strings_are_scrubbed_but_numbers_and_flags_pass() -> None:
    kept, _ = TraceRedactor("key").clean("write_lookup", {"tool": "crear_reclamo", "found": False})
    assert kept == {"tool": "crear_reclamo", "found": False}

    kept, _ = TraceRedactor("key").clean(
        "tool_rejected", {"tool": "x", "reason": "r", "detail": "a@b.co"}
    )
    assert kept["detail"] == REDACTED


def test_the_principal_is_a_stable_keyed_pseudonym() -> None:
    first, again, other_key = TraceRedactor("a"), TraceRedactor("a"), TraceRedactor("b")
    pseudonym = first.principal("comercio_a", "operador_a")

    assert pseudonym == again.principal("comercio_a", "operador_a")
    assert pseudonym != other_key.principal("comercio_a", "operador_a")
    assert pseudonym != first.principal("comercio_a", "operador_b")
    assert re.fullmatch(r"p_[0-9a-f]{10}", pseudonym)
    assert "operador" not in pseudonym


def test_the_same_principal_id_in_two_companies_gets_two_pseudonyms() -> None:
    redactor = TraceRedactor("a")

    assert redactor.principal("comercio_a", "admin") != redactor.principal("comercio_b", "admin")


def test_the_tenant_and_the_principal_cannot_be_shifted_into_each_other() -> None:
    redactor = TraceRedactor("a")

    assert redactor.principal("ab", "c") != redactor.principal("a", "bc")


def test_the_pseudonym_is_an_hmac_sha256_of_tenant_and_principal_under_the_key() -> None:
    expected = hmac.new(b"the-key", b"comercio_a\0operador_a", hashlib.sha256).hexdigest()

    assert TraceRedactor("the-key").principal("comercio_a", "operador_a") == f"p_{expected[:10]}"


def test_the_default_key_gives_stable_pseudonyms() -> None:
    first, again = TraceRedactor(DEFAULT_TRACE_KEY), TraceRedactor(DEFAULT_TRACE_KEY)

    assert first.principal("comercio_a", "operador_a") == again.principal(
        "comercio_a", "operador_a"
    )


@pytest.mark.parametrize("key", ["", " ", "\t\n  "])
def test_a_key_is_required(key: str) -> None:
    with pytest.raises(ValueError, match="key"):
        TraceRedactor(key)


def _emitted() -> list[tuple[str, set[str]]]:
    """Every (event type, attribute names) written with a literal type anywhere in the source."""
    found = []
    for module in SOURCE.rglob("*.py"):
        for node in ast.walk(ast.parse(module.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr not in {"_trace", "emit", "trace"} or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                names = {kw.arg for kw in node.keywords if kw.arg and kw.arg != "step"}
                found.append((first.value, names))
    return found


def test_every_event_the_code_emits_is_allowlisted_with_its_attributes() -> None:
    emitted = _emitted()

    assert len(emitted) > 20  # the scan really found the call sites
    for event_type, names in emitted:
        assert event_type in ALLOWED_ATTRIBUTES, f"{event_type} is not allowlisted"
        assert names <= ALLOWED_ATTRIBUTES[event_type], (
            f"{event_type} carries {sorted(names - ALLOWED_ATTRIBUTES[event_type])} "
            "outside its allowlist"
        )
