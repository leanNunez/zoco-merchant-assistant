"""The three tools of Zoco, reused from the first delivery (brief section 3).

The argument models are the first delivery's strict pydantic models (no coercion, no extra
fields). `crear_reclamo` adds the `operation_id` the kit requires, which makes a repeated
command recognizable.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

from cortex.identifiers import OPERATION_ID_PATTERN
from cortex.tools.catalog import Catalog, ToolKind, ToolSpec
from merchant_assistant.agent.domain.tools import (
    BUSCAR_DOCUMENTACION,
    CONSULTAR_VENTAS,
    CREAR_RECLAMO,
    TOOL_SPECS,
    BuscarDocumentacionArgs,
    ConsultarVentasArgs,
    CrearReclamoArgs,
)
from merchant_assistant.sales.contract import parse_sales_date


class CrearReclamoOperationArgs(CrearReclamoArgs):
    """The first delivery's complaint arguments plus the idempotency key of the command."""

    operation_id: Annotated[
        str,
        StringConstraints(pattern=OPERATION_ID_PATTERN),
        Field(description="The id of this exact command; repeating it never files a second claim."),
    ]


def _sales_precheck(arguments: BaseModel, today: date) -> None:
    if not isinstance(arguments, ConsultarVentasArgs):
        raise TypeError("consultar_ventas received arguments of another tool")
    # Raises InvalidSalesDate or FutureSalesDate: the adapter never sees such a date.
    parse_sales_date(arguments.fecha, today=today)


def zoco_catalog() -> Catalog:
    return Catalog(
        (
            ToolSpec(
                name=BUSCAR_DOCUMENTACION,
                description=TOOL_SPECS[BUSCAR_DOCUMENTACION].description,
                kind=ToolKind.READ,
                permission="docs:read",
                resource="documents",
                args_model=BuscarDocumentacionArgs,
                requires_confirmation=False,
            ),
            ToolSpec(
                name=CONSULTAR_VENTAS,
                description=TOOL_SPECS[CONSULTAR_VENTAS].description,
                kind=ToolKind.READ,
                permission="sales:read",
                resource="sales",
                args_model=ConsultarVentasArgs,
                requires_confirmation=False,
                precheck=_sales_precheck,
            ),
            ToolSpec(
                name=CREAR_RECLAMO,
                description=(
                    "File a complaint for the merchant. It runs only after the merchant confirms "
                    "these exact values; 'operation_id' identifies the command so a repetition "
                    "never files it twice."
                ),
                kind=ToolKind.WRITE,
                permission="claims:write",
                resource="claims",
                args_model=CrearReclamoOperationArgs,
                requires_confirmation=True,
            ),
        )
    )
