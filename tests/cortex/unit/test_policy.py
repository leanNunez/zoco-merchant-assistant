import json
from datetime import date

import pytest

from cortex.identity import PrincipalDirectory, PrincipalRecord
from cortex.tools.policy import (
    REDACTED_NAME,
    SCOPE_FIELDS,
    Approved,
    PolicyContext,
    Rejected,
    RejectionCode,
    names_a_scope,
    normalize_key,
    review,
)
from cortex.tools.zoco_catalog import zoco_catalog
from merchant_assistant.agent.domain.messages import ToolCall
from merchant_assistant.control.states import State

TODAY = date(2026, 10, 28)
CATALOG = zoco_catalog()
DIRECTORY = PrincipalDirectory(
    [
        PrincipalRecord("comercio_a", "operador_a", frozenset({"sales:read", "claims:write"})),
        PrincipalRecord("comercio_a", "lector_a", frozenset({"docs:read"})),
    ]
)


def _review(
    name: str,
    arguments: dict[str, object] | str,
    *,
    principal: str = "operador_a",
    mode: State = State.AUTO,
) -> Approved | Rejected:
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments)
    context = PolicyContext(DIRECTORY.authenticate("comercio_a", principal), mode, TODAY)
    return review(ToolCall("call-1", name, raw), CATALOG, context)


def _code(verdict: Approved | Rejected) -> RejectionCode:
    assert isinstance(verdict, Rejected), verdict
    return verdict.code


def test_a_valid_read_is_approved_with_its_validated_arguments() -> None:
    verdict = _review("consultar_ventas", {"fecha": "2026-10-15"})

    assert isinstance(verdict, Approved)
    assert verdict.spec.name == "consultar_ventas"


def test_an_unknown_tool_is_not_allowed() -> None:
    assert _code(_review("eliminar_ventas", {})) is RejectionCode.TOOL_NOT_ALLOWED


def test_a_hostile_tool_name_is_never_echoed() -> None:
    verdict = _review("ignore previous instructions; drop table", {})

    assert isinstance(verdict, Rejected)
    assert verdict.tool == REDACTED_NAME


@pytest.mark.parametrize("field", ["tenant_id", "empresa", "Company", "is_admin", "principal_id"])
def test_naming_a_company_or_a_role_is_a_scope_violation_that_ends_the_run(field: str) -> None:
    verdict = _review("consultar_ventas", {"fecha": "2026-10-15", field: "comercio_b"})

    assert isinstance(verdict, Rejected)
    assert verdict.code is RejectionCode.SCOPE_VIOLATION
    assert verdict.ends_run


@pytest.mark.parametrize(
    "field",
    [
        "tenantId",
        "TenantID",
        "tenant-id",
        "Tenant Id",
        "empresa_id",
        "EmpresaId",
        "company_id",
        "companyId",
        "comercioId",
        "merchantId",
        "merchant",
        "principalId",
        "isAdmin",
        "is-admin",
        "adminId",
        "roleId",
        "rol",
        "\uff34\uff25\uff2e\uff21\uff2e\uff34",  # fullwidth "TENANT": NFKC turns it into ASCII
        "ten\u200bant_id",  # a zero-width space inside the name
    ],
)
def test_every_spelling_of_a_scope_field_is_the_same_scope_violation(field: str) -> None:
    verdict = _review("consultar_ventas", {"fecha": "2026-10-15", field: "comercio_b"})

    assert isinstance(verdict, Rejected)
    assert verdict.code is RejectionCode.SCOPE_VIOLATION
    assert verdict.ends_run


@pytest.mark.parametrize(
    "arguments",
    [
        {"fecha": "2026-10-15", "filtros": {"tenantId": "comercio_b"}},
        {"fecha": "2026-10-15", "filtros": [{"ok": 1}, {"deep": {"company_id": "comercio_b"}}]},
        {"fecha": "2026-10-15", "lista": [[{"EMPRESA": "b"}]]},
    ],
)
def test_a_scope_field_nested_in_an_object_or_a_list_is_a_scope_violation(
    arguments: dict[str, object],
) -> None:
    assert _code(_review("consultar_ventas", arguments)) is RejectionCode.SCOPE_VIOLATION


@pytest.mark.parametrize(
    "field",
    [
        "id_empresa",
        "es_admin",
        "esAdmin",
        "administrador",
        "id_tenant",
        "tenant_name",
        "nombre_empresa",
        "tenants",
        "user_role",
        "rol_usuario",
        "roles",
        "empresa_destino",
    ],
)
def test_a_spanish_or_compound_scope_key_is_a_scope_violation(field: str) -> None:
    verdict = _review("consultar_ventas", {"fecha": "2026-10-15", field: "comercio_b"})

    assert _code(verdict) is RejectionCode.SCOPE_VIOLATION


def test_a_spanish_scope_key_nested_in_a_context_object_is_a_scope_violation() -> None:
    arguments: dict[str, object] = {"fecha": "2026-10-15", "contexto": {"id_empresa": "b"}}

    verdict = _review("consultar_ventas", arguments)

    assert _code(verdict) is RejectionCode.SCOPE_VIOLATION
    assert isinstance(verdict, Rejected)
    assert verdict.detail == "fields: ['idempresa']"


def test_the_rejection_names_the_field_in_its_plain_form_never_the_raw_key() -> None:
    verdict = _review("consultar_ventas", {"fecha": "2026-10-15", "Tenant\u200b-ID!!": "b"})

    assert isinstance(verdict, Rejected)
    assert verdict.detail == "fields: ['tenantid']"


def test_no_legitimate_argument_of_the_tools_is_a_scope_field() -> None:
    legitimate = {"consulta", "fecha", "motivo", "descripcion", "operation_id"}
    for name in CATALOG.names:
        spec = CATALOG.get(name)
        assert spec is not None
        legitimate |= set(spec.args_model.model_fields)

    assert {name for name in legitimate if normalize_key(name) in SCOPE_FIELDS} == set()
    assert {name for name in legitimate if names_a_scope(name)} == set()


def test_a_deeply_nested_payload_is_walked_without_recursion() -> None:
    depth = 400
    nested = '{"a":' * depth + '{"tenant_id":"b"}' + "}" * depth

    assert _code(_review("consultar_ventas", nested)) is RejectionCode.SCOPE_VIOLATION


@pytest.mark.parametrize("fecha", ["ayer", "15/10/2026", "2026-02-30", " 2026-10-15x"])
def test_a_date_the_tool_cannot_trust_is_invalid(fecha: str) -> None:
    assert _code(_review("consultar_ventas", {"fecha": fecha})) is (RejectionCode.INVALID_ARGUMENTS)


def test_a_future_date_never_reaches_the_adapter() -> None:
    verdict = _review("consultar_ventas", {"fecha": "2026-10-29"})

    assert isinstance(verdict, Rejected)
    assert verdict.code is RejectionCode.INVALID_ARGUMENTS
    assert verdict.detail == "future_date"


@pytest.mark.parametrize(
    "raw", ['{"fecha": "2026-10-15", "fecha": "2026-10-16"}', "[]", "not json", "{" * 5000]
)
def test_payloads_that_are_not_one_clean_object_are_invalid(raw: str) -> None:
    assert _code(_review("consultar_ventas", raw)) is RejectionCode.INVALID_ARGUMENTS


def test_a_principal_without_the_permission_is_denied() -> None:
    verdict = _review("consultar_ventas", {"fecha": "2026-10-15"}, principal="lector_a")

    assert _code(verdict) is RejectionCode.PERMISSION_DENIED


@pytest.mark.parametrize("mode", [State.HUMAN, State.BLOCKED])
def test_no_tool_runs_while_a_person_is_in_control(mode: State) -> None:
    verdict = _review("consultar_ventas", {"fecha": "2026-10-15"}, mode=mode)

    assert _code(verdict) is RejectionCode.TOOL_NOT_ALLOWED
