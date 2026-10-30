"""Composition root: the only place where the harness parts are wired together.

The kit's fixtures play the part of the companies' systems of record: who belongs to which
company, their sales and contacts, and the tools' contract. Everything is checked when loaded,
so a broken or loosened kit stops the harness before any run.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import MappingProxyType
from zoneinfo import ZoneInfo

from cortex.config import ConfigError, HarnessConfig, load_harness_config
from cortex.identity import AuthContext, PrincipalDirectory, PrincipalRecord
from cortex.memory.database import Database
from cortex.memory.tenant_store import Contact
from cortex.observability.redaction import TraceRedactor
from cortex.runner.agent_runner import AgentRunner
from cortex.runner.finish import FinishReason
from cortex.runner.model import ModelPort
from cortex.timekeeping import Ticker
from cortex.tools.adapters.claims import ClaimsDesk
from cortex.tools.adapters.docs import DocsLibrary
from cortex.tools.adapters.sales import SalesBook
from cortex.tools.catalog import Catalog
from cortex.tools.gateway import ToolAdapter, ToolBinder
from cortex.tools.zoco_catalog import zoco_catalog
from merchant_assistant.agent.domain.tools import (
    BUSCAR_DOCUMENTACION,
    CONSULTAR_VENTAS,
    CREAR_RECLAMO,
)
from merchant_assistant.shared.clock import Clock, business_today

BUSINESS_TIMEZONE = ZoneInfo("America/Argentina/Buenos_Aires")

# Key of the principal pseudonym in traces. Not a secret in this demo (principals are fixtures),
# which keeps the sample traces reproducible; a deployment passes its own to `build_runner`
# (the entry point reads it from CORTEX_TRACE_KEY).
DEFAULT_TRACE_KEY = "cortex-trace-v1"


@dataclass(frozen=True, slots=True)
class HarnessData:
    config: HarnessConfig
    directory: PrincipalDirectory
    # The principal the kit uses for each company when a scenario does not name one.
    test_principals: Mapping[str, str]
    sales: SalesBook
    docs: DocsLibrary
    catalog: Catalog
    # Each company's contacts, by company id. The world loads them into each company's store.
    contacts: Mapping[str, tuple[Contact, ...]]


def load_harness_data(kit_dir: Path, corpora: Mapping[str, Path]) -> HarnessData:
    """Load and check the kit's fixtures. `corpora` maps each company to the directory of the
    documents it may search; a company that is not in it has none."""
    config = load_harness_config(_read_json(kit_dir / "harness_config.json"))
    if config.allowed_finish_reasons != {reason.value for reason in FinishReason}:
        raise ConfigError("the kit's finish reasons differ from the harness's")

    companies = _read_json(kit_dir / "fixtures" / "tenants_ventas.json")
    records = [
        PrincipalRecord(
            tenant_id=str(tenant["tenant_id"]),
            principal_id=str(tenant["test_principal"]),
            permissions=_strings(tenant["permissions"]),
        )
        for tenant in _rows(companies, "tenants")
    ]
    directory = PrincipalDirectory(records)
    sales = SalesBook.from_fixture(_rows(companies, "sales"))
    contacts: dict[str, tuple[Contact, ...]] = {}
    for row in _rows(companies, "contacts"):
        tenant_id = str(row["tenant_id"])
        if tenant_id not in {record.tenant_id for record in records}:
            raise ConfigError(f"a contact names an unknown company: {tenant_id!r}")
        contact = Contact(str(row["contact_id"]), str(row["name"]))
        contacts[tenant_id] = (*contacts.get(tenant_id, ()), contact)

    catalog = zoco_catalog()
    catalog.verify_against(_rows(_read_json(kit_dir / "tools_config.json"), "tools"))
    return HarnessData(
        config=config,
        directory=directory,
        test_principals=MappingProxyType({r.tenant_id: r.principal_id for r in records}),
        sales=sales,
        docs=DocsLibrary(corpora),
        catalog=catalog,
        contacts=MappingProxyType(contacts),
    )


def zoco_tool_binder(data: HarnessData, claims: ClaimsDesk, *, clock: Clock) -> ToolBinder:
    """The adapters of one company: its documents, its sales and its claims file, nothing else.
    `clock` gives the business date a document's validity is judged on."""

    def today() -> date:
        return business_today(clock, BUSINESS_TIMEZONE)

    def bind(auth: AuthContext) -> Mapping[str, ToolAdapter]:
        return {
            BUSCAR_DOCUMENTACION: data.docs.for_tenant(auth, today=today),
            CONSULTAR_VENTAS: data.sales.for_tenant(auth),
            CREAR_RECLAMO: claims.for_tenant(auth),
        }

    return bind


def build_runner(
    *,
    data: HarnessData,
    database: Database,
    model: ModelPort,
    bind_tools: ToolBinder,
    clock: Clock,
    ticker: Ticker,
    trace_key: str = DEFAULT_TRACE_KEY,
    real_wait_floor_ms: int = 0,
) -> AgentRunner:
    """`real_wait_floor_ms` is the shortest real wait of a tool attempt or a model call: 0 unless
    `ticker` is simulated (see `AgentRunner.real_wait_floor_ms`)."""
    return AgentRunner(
        database=database,
        catalog=data.catalog,
        bind_tools=bind_tools,
        model=model,
        clock=clock,
        ticker=ticker,
        business_tz=BUSINESS_TIMEZONE,
        redactor=TraceRedactor(trace_key),
        real_wait_floor_ms=real_wait_floor_ms,
    )


def _read_json(path: Path) -> dict[str, object]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name} must hold a JSON object")
    return data


def _strings(value: object) -> frozenset[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError("permissions must be a list of strings")
    return frozenset(value)


def _rows(data: Mapping[str, object], key: str) -> list[Mapping[str, object]]:
    rows = data.get(key)
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ConfigError(f"{key} must be a list of objects")
    return rows
