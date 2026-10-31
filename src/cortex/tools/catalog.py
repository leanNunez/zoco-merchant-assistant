"""Declarative tool specifications (brief section 3).

Each tool declares its argument schema, the permission it needs, the resource it touches,
whether it reads or writes, and whether it needs an exact confirmation. A catalog is immutable
and built per harness, never global, and it can be checked against the kit's `tools_config.json`
so the code and the contract cannot drift apart.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from types import MappingProxyType

from pydantic import BaseModel

from cortex.config import ConfigError

# A check of the validated arguments that needs domain knowledge, such as "not a future date".
# It runs before the adapter and raises a DomainError to refuse.
Precheck = Callable[[BaseModel, date], None]


class ToolKind(StrEnum):
    READ = "read"
    WRITE = "write"


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    kind: ToolKind
    permission: str
    resource: str
    args_model: type[BaseModel]
    requires_confirmation: bool
    tenant_scoped: bool = True
    precheck: Precheck | None = None

    def __post_init__(self) -> None:
        if self.kind is ToolKind.WRITE and not self.requires_confirmation:
            # The strictest rule wins: the global policy confirms every write.
            raise ValueError(f"{self.name}: a write tool always requires an exact confirmation")


class Catalog:
    def __init__(self, specs: Iterable[ToolSpec]) -> None:
        by_name: dict[str, ToolSpec] = {}
        for spec in specs:
            if spec.name in by_name:
                raise ValueError(f"duplicate tool: {spec.name}")
            by_name[spec.name] = spec
        self._specs: Mapping[str, ToolSpec] = MappingProxyType(by_name)

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._specs)

    def restricted_to(self, names: Iterable[str]) -> Catalog:
        """The same catalog without the tools not in `names`."""
        keep = set(names)
        return Catalog(spec for name, spec in self._specs.items() if name in keep)

    def verify_against(self, declarations: Sequence[Mapping[str, object]]) -> None:
        """Refuse to start if the code's tools differ from the contract's declarations."""
        declared = {str(item.get("name")): item for item in declarations}
        if set(declared) != set(self._specs):
            raise ConfigError(f"tools differ: kit {sorted(declared)}, code {sorted(self._specs)}")
        for name, item in declared.items():
            spec = self._specs[name]
            schema = item.get("args_schema")
            expected = {
                "kind": spec.kind.value,
                "required_permission": spec.permission,
                "tenant_scoped": spec.tenant_scoped,
            }
            for key, value in expected.items():
                if item.get(key) != value:
                    raise ConfigError(f"{name}.{key}: kit {item.get(key)!r}, code {value!r}")
            if not isinstance(schema, dict) or set(schema) != set(spec.args_model.model_fields):
                raise ConfigError(f"{name}: the argument names differ from the kit")
            if (
                item.get("requires_confirmed_exact_action") is True
                and not spec.requires_confirmation
            ):
                raise ConfigError(f"{name}: the kit requires a confirmation the code does not ask")
