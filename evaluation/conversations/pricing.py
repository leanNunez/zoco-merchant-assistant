"""What a live run costs, from the served model and the token counts the provider reports.

A model without a price here has an UNKNOWN cost, never zero: a spending cap that counted it as
free would let a run spend without limit. The live mode refuses to start for a requested model
it cannot price.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

MILLION = Decimal(1_000_000)


@dataclass(frozen=True, slots=True)
class Price:
    input_per_million: Decimal  # USD per million prompt tokens
    output_per_million: Decimal  # USD per million completion tokens


# Verify against the provider's price list before a live run: prices change.
PRICES: dict[str, Price] = {
    "gpt-4o-mini": Price(Decimal("0.15"), Decimal("0.60")),
}

_SNAPSHOT = re.compile(r"^(?P<family>.+)-[0-9]{4}-[0-9]{2}-[0-9]{2}$")


def family(model: str) -> str:
    """`gpt-4o-mini-2024-07-18` (a dated snapshot the provider serves) is `gpt-4o-mini`."""
    match = _SNAPSHOT.match(model)
    return model if match is None else match.group("family")


def price_for(model: str) -> Price | None:
    return PRICES.get(model) or PRICES.get(family(model))


def is_same_model(requested: str, served: str) -> bool:
    """The served model is the one requested, or a dated snapshot of it."""
    return served == requested or family(served) == requested


def cost(model: str, prompt_tokens: int | None, completion_tokens: int | None) -> Decimal | None:
    """The cost of one call in USD, or None when the price or the usage is unknown."""
    price = price_for(model)
    if price is None or prompt_tokens is None or completion_tokens is None:
        return None
    return (
        Decimal(prompt_tokens) * price.input_per_million
        + Decimal(completion_tokens) * price.output_per_million
    ) / MILLION
