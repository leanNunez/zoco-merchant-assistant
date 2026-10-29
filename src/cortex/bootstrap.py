"""Composition root: the only place where the harness parts are wired together.

The kit's fixtures play the part of the companies' systems of record: who belongs to which
company, their sales, and the tools' contract. Everything is checked when loaded, so a broken or
loosened kit stops the harness before any run.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from cortex.config import ConfigError, HarnessConfig, load_harness_config
from cortex.identity import AuthContext, PrincipalDirectory, PrincipalRecord
from cortex.memory.database import Database
from cortex.runner.agent_runner import AgentRunner
from cortex.runner.budget import Ticker
from cortex.runner.finish import FinishReason
from cortex.runner.model import ModelPort
from cortex.tools.adapters.sales import SalesBook
from cortex.tools.catalog import Catalog
from cortex.tools.gateway import ToolAdapter, ToolBinder
from cortex.tools.zoco_catalog import zoco_catalog
from merchant_assistant.agent.domain.tools import CONSULTAR_VENTAS
from merchant_assistant.shared.clock import Clock

BUSINESS_TIMEZONE = ZoneInfo("America/Argentina/Buenos_Aires")


@dataclass(frozen=True, slots=True)
class HarnessData:
    config: HarnessConfig
    directory: PrincipalDirectory
    sales: SalesBook
    catalog: Catalog


def load_harness_data(kit_dir: Path) -> HarnessData:
    config = load_harness_config(_read_json(kit_dir / "harness_config.json"))
    if config.allowed_finish_reasons != {reason.value for reason in FinishReason}:
        raise ConfigError("the kit's finish reasons differ from the harness's")

    companies = _read_json(kit_dir / "fixtures" / "tenants_ventas.json")
    directory = PrincipalDirectory(
        PrincipalRecord(
            tenant_id=str(tenant["tenant_id"]),
            principal_id=str(tenant["test_principal"]),
            permissions=_strings(tenant["permissions"]),
        )
        for tenant in _rows(companies, "tenants")
    )
    sales = SalesBook.from_fixture(_rows(companies, "sales"))

    catalog = zoco_catalog()
    catalog.verify_against(_rows(_read_json(kit_dir / "tools_config.json"), "tools"))
    return HarnessData(config=config, directory=directory, sales=sales, catalog=catalog)


def zoco_tool_binder(data: HarnessData) -> ToolBinder:
    def bind(auth: AuthContext) -> Mapping[str, ToolAdapter]:
        return {CONSULTAR_VENTAS: data.sales.for_tenant(auth)}

    return bind


def build_runner(
    *,
    data: HarnessData,
    database: Database,
    model: ModelPort,
    bind_tools: ToolBinder,
    clock: Clock,
    ticker: Ticker,
) -> AgentRunner:
    return AgentRunner(
        database=database,
        catalog=data.catalog,
        bind_tools=bind_tools,
        model=model,
        clock=clock,
        ticker=ticker,
        business_tz=BUSINESS_TIMEZONE,
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
