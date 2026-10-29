import ast
from pathlib import Path

import pytest

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


def test_a_value_is_cut_to_its_limit() -> None:
    sentence = "una frase normal " * 40
    assert len(scrub(sentence)) == 120
    assert len(scrub(sentence, limit=200)) == 200


def test_masking_reaches_into_nested_values() -> None:
    cleaned = scrub_tree({"a": ["mail x@y.com", {"b": "ok"}], "n": 3})

    assert cleaned == {"a": [f"mail {REDACTED}", {"b": "ok"}], "n": 3}


def test_only_allowlisted_attributes_are_kept_and_the_rest_is_counted() -> None:
    redactor = TraceRedactor("salt")

    kept, dropped = redactor.clean(
        "tool_requested", {"tool": "consultar_ventas", "arguments": "fecha=2026", "x": 1}
    )

    assert kept == {"tool": "consultar_ventas"}
    assert dropped == 2


def test_an_event_type_nobody_allowed_carries_nothing() -> None:
    assert TraceRedactor("salt").clean("brand_new_event", {"anything": "at all"}) == ({}, 1)


def test_strings_are_scrubbed_but_numbers_and_flags_pass() -> None:
    kept, _ = TraceRedactor("salt").clean("write_lookup", {"tool": "crear_reclamo", "found": False})
    assert kept == {"tool": "crear_reclamo", "found": False}

    kept, _ = TraceRedactor("salt").clean(
        "tool_rejected", {"tool": "x", "reason": "r", "detail": "a@b.co"}
    )
    assert kept["detail"] == REDACTED


def test_the_principal_is_a_stable_salted_hash() -> None:
    first, again, other_salt = TraceRedactor("a"), TraceRedactor("a"), TraceRedactor("b")

    assert first.principal("operador_a") == again.principal("operador_a")
    assert first.principal("operador_a") != other_salt.principal("operador_a")
    assert first.principal("operador_a") != first.principal("operador_b")
    assert "operador" not in first.principal("operador_a")


def test_a_salt_is_required() -> None:
    with pytest.raises(ValueError, match="salt"):
        TraceRedactor("")


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
