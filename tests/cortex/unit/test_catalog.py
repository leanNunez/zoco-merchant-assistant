import copy
import json
from pathlib import Path

import pytest

from cortex.config import ConfigError
from cortex.tools.catalog import ToolKind, ToolSpec
from cortex.tools.zoco_catalog import zoco_catalog
from merchant_assistant.agent.domain.tools import ConsultarVentasArgs

KIT = Path(__file__).resolve().parents[3] / "kit"
KIT_TOOLS = json.loads((KIT / "tools_config.json").read_text(encoding="utf-8"))["tools"]


def test_the_code_declares_the_same_tools_as_the_contract() -> None:
    zoco_catalog().verify_against(KIT_TOOLS)


@pytest.mark.parametrize(
    ("index", "key", "value"),
    [
        (1, "required_permission", "sales:admin"),
        (1, "kind", "write"),
        (0, "tenant_scoped", False),
        (2, "args_schema", {"motivo": "string", "descripcion": "string"}),
    ],
)
def test_any_drift_from_the_contract_stops_the_harness(index: int, key: str, value: object) -> None:
    drifted = copy.deepcopy(KIT_TOOLS)
    drifted[index][key] = value

    with pytest.raises(ConfigError):
        zoco_catalog().verify_against(drifted)


def test_a_write_tool_without_confirmation_cannot_even_be_declared() -> None:
    with pytest.raises(ValueError, match="always requires an exact confirmation"):
        ToolSpec(
            name="borrar",
            description="",
            kind=ToolKind.WRITE,
            permission="x:write",
            resource="x",
            args_model=ConsultarVentasArgs,
            requires_confirmation=False,
        )
