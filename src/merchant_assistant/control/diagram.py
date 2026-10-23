"""The state diagram, generated from the rule table so it can never drift from the code.

    python -m merchant_assistant.control.diagram > design/state-diagram.md

`make diagram` does exactly that, and a test fails if the committed file is stale.
"""

from __future__ import annotations

import sys

from merchant_assistant.control.rules import RULES, Rule
from merchant_assistant.control.states import Effect, State

GUARD_MARKER = "- open hours only"


def _edge(rule: Rule) -> str:
    # Sorted by name so the output never depends on set iteration order.
    actors = "/".join(sorted(actor.value for actor in rule.actors))
    label = f"{rule.event.value} ({actors})"
    if rule.requires_open_hours:
        label = f"{label} {GUARD_MARKER}"
    return f"    {rule.source.value} --> {rule.target.value}: {label}"


def render_mermaid() -> str:
    """A `stateDiagram-v2`, with one edge per rule in the order of the rule table."""
    lines = ["stateDiagram-v2", f"    [*] --> {State.AUTO.value}"]
    lines.extend(_edge(rule) for rule in RULES)
    return "\n".join(lines) + "\n"


_EFFECT_MEANING = {
    Effect.INVALIDATE_PENDING_ACTION: (
        "Todo cambio de estado",
        "Descarta la acción pendiente (por ejemplo, un reclamo a medio confirmar).",
    ),
    Effect.DISCARD_UNAPPROVED_DRAFTS: (
        "Al salir de SUGGEST",
        "Descarta los borradores que nadie aprobó, para que no se envíen después.",
    ),
    Effect.NOTIFY_OPERATOR: (
        "Abuso detectado en HUMAN",
        "Avisa al operador, que decide si bloquea (`operator_block`). El estado no cambia.",
    ),
}


def render_document() -> str:
    """The Markdown page committed as `design/state-diagram.md`."""
    effect_rows = "\n".join(
        f"| `{effect.value}` | {when} | {what} |"
        for effect, (when, what) in _EFFECT_MEANING.items()
    )
    return (
        "# Diagrama de estados IA-humano\n"
        "\n"
        "Generado por `make diagram` a partir de la tabla de reglas de "
        "`src/merchant_assistant/control/rules.py`. No se edita a mano: un test falla si "
        "este archivo no coincide con el código.\n"
        "\n"
        "- `[*] --> AUTO`: toda conversación empieza en AUTO.\n"
        "- Cada flecha dice `evento (actores que pueden dispararlo)`.\n"
        f"- Una flecha que termina en `{GUARD_MARKER}` solo se permite dentro de la franja "
        "de atención humana; fuera de ella el evento se rechaza con el código `out_of_hours`.\n"
        "- Cualquier par (estado, evento) que no figure como flecha es indefinido y "
        "falla con `invalid_transition`.\n"
        "\n"
        "```mermaid\n"
        f"{render_mermaid()}"
        "```\n"
        "\n"
        "## Efectos que la aplicación debe ejecutar\n"
        "\n"
        "Son instrucciones declarativas: el dominio las devuelve y no las ejecuta.\n"
        "\n"
        "| Efecto | Cuándo | Qué hace |\n"
        "|---|---|---|\n"
        f"{effect_rows}\n"
    )


def main() -> None:
    sys.stdout.write(render_document())


if __name__ == "__main__":
    main()
