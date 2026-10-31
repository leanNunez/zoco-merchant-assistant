import pytest

from cortex.identity import AuthContext, AuthenticationFailed, PrincipalDirectory, PrincipalRecord

DIRECTORY = PrincipalDirectory(
    [
        PrincipalRecord("comercio_a", "operador_a", frozenset({"sales:read"})),
        PrincipalRecord("comercio_b", "operador_b", frozenset({"sales:read", "claims:write"})),
    ]
)


def test_a_principal_is_authenticated_for_its_own_company() -> None:
    auth = DIRECTORY.authenticate("comercio_a", "operador_a")

    assert (auth.tenant_id, auth.principal_id) == ("comercio_a", "operador_a")
    assert auth.can("sales:read")
    assert not auth.can("claims:write")


@pytest.mark.parametrize(
    ("tenant", "principal"),
    [
        ("comercio_a", "operador_b"),  # a real principal, but of the other company
        ("comercio_c", "operador_a"),
        ("comercio_a", "admin"),
    ],
)
def test_anything_else_fails_with_one_indistinct_error(tenant: str, principal: str) -> None:
    with pytest.raises(AuthenticationFailed) as refused:
        DIRECTORY.authenticate(tenant, principal)

    assert refused.value.public() == {
        "code": "auth_invalid",
        "message": "The request could not be authenticated.",
    }


def test_an_auth_context_cannot_be_built_outside_the_directory() -> None:
    with pytest.raises(TypeError, match="only by PrincipalDirectory"):
        AuthContext("comercio_a", "operador_a", frozenset(), _proof=object())


@pytest.mark.parametrize("bad", ["Comercio_A", "comercio-a", "", "a" * 65, "comercio a", "../x"])
def test_identifiers_use_a_closed_alphabet(bad: str) -> None:
    with pytest.raises(ValueError, match="not a valid identifier"):
        PrincipalRecord(bad, "operador_a", frozenset())
