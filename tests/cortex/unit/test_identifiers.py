"""The closed alphabets and length caps of every identifier the harness accepts, in one place."""

import re

import pytest

from cortex import identifiers
from cortex.identity import PrincipalRecord
from cortex.memory.tenant_store import check_external_id
from cortex.tools.zoco_catalog import CrearReclamoOperationArgs


def test_the_patterns_are_exactly_the_ones_the_harness_always_had() -> None:
    # Moving the patterns must not change what any id accepts or rejects.
    assert identifiers.IDENTIFIER.pattern == r"[a-z0-9][a-z0-9_]{0,63}"
    assert identifiers.EXTERNAL_ID.pattern == r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}"
    assert identifiers.OPERATION_ID_PATTERN == r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$"
    assert identifiers.PLAIN_TOOL_NAME.pattern == r"[A-Za-z_][A-Za-z0-9_]{0,63}"


@pytest.mark.parametrize(
    ("pattern", "alphabet_ok", "limit"),
    [
        (identifiers.IDENTIFIER, "a", identifiers.MAX_IDENTIFIER_CHARS),
        (identifiers.EXTERNAL_ID, "A", identifiers.MAX_EXTERNAL_ID_CHARS),
        (identifiers.PLAIN_TOOL_NAME, "_", identifiers.MAX_TOOL_NAME_CHARS),
    ],
)
def test_the_length_cap_is_the_named_constant(
    pattern: object, alphabet_ok: str, limit: int
) -> None:
    assert isinstance(pattern, re.Pattern)
    assert pattern.fullmatch(alphabet_ok * limit) is not None
    assert pattern.fullmatch(alphabet_ok * (limit + 1)) is None


def test_the_caps_are_the_documented_values() -> None:
    assert (
        identifiers.MAX_IDENTIFIER_CHARS,
        identifiers.MAX_EXTERNAL_ID_CHARS,
        identifiers.MAX_OPERATION_ID_CHARS,
        identifiers.MAX_TOOL_NAME_CHARS,
    ) == (64, 128, 64, 64)


def test_the_modules_that_validate_use_the_shared_patterns() -> None:
    assert PrincipalRecord("a" * 64, "b" * 64, frozenset()).tenant_id == "a" * 64
    with pytest.raises(ValueError, match="not a valid identifier"):
        PrincipalRecord("a" * 65, "b", frozenset())
    assert check_external_id("c" * 128) == "c" * 128
    with pytest.raises(ValueError, match="not a valid id"):
        check_external_id("c" * 129)


@pytest.mark.parametrize(("length", "ok"), [(64, True), (65, False)])
def test_an_operation_id_is_capped_at_the_named_length(length: int, ok: bool) -> None:
    arguments = {
        "motivo": "demora",
        "descripcion": "No recibi comprobante",
        "operation_id": "o" * length,
    }

    if ok:
        assert CrearReclamoOperationArgs.model_validate(arguments).operation_id == "o" * length
    else:
        with pytest.raises(ValueError, match="operation_id"):
            CrearReclamoOperationArgs.model_validate(arguments)
