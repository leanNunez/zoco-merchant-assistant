"""Regenerate data/ventas.json, the sales dataset the `consultar_ventas` tool reads.

Deterministic: a seeded generator and no reading of the clock, so running it twice
produces the same bytes. The shape is chosen to exercise the cases the brief names:

- 37 days of history (2026-09-15 to 2026-10-22) with ONE day missing: no record at all,
  which must be reported as "no sales registered", not as zero sales;
- one registered day with zero sales, the contrast to that gap;
- one day whose refunds exceed its gross sales, so the net is negative;
- one record in the future.
"""

from __future__ import annotations

import json
import random
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

SEED = 20261023
FIRST_DAY = date(2026, 9, 15)
LAST_DAY = date(2026, 10, 22)
GAP_DAY = date(2026, 9, 27)  # a Sunday
ZERO_SALES_DAY = date(2026, 9, 28)  # registered, but nothing was sold
NEGATIVE_NET_DAY = date(2026, 10, 9)
# A presale. It is far enough away to stay in the future for any date this project can be
# evaluated on, so the "future date" case does not expire.
PRESALE_DAY = date(2027, 12, 15)
OUTPUT = Path(__file__).resolve().parent.parent / "data" / "ventas.json"


def _money(amount: Decimal) -> str:
    return format(amount.quantize(Decimal("0.01")), "f")


def _row(day: date, tickets: int, gross: Decimal, refunds: Decimal) -> dict[str, Any]:
    return {
        "fecha": day.isoformat(),
        "entradas": tickets,
        "bruto": _money(gross),
        "devoluciones": _money(refunds),
        "neto": _money(gross - refunds),
    }


def build_document() -> dict[str, Any]:
    rng = random.Random(SEED)  # noqa: S311 - a reproducible dataset, not security
    rows: list[dict[str, Any]] = []
    day = FIRST_DAY
    while day <= LAST_DAY:
        # Draw the same numbers for every day, special or not, so changing one special day
        # never reshuffles the others.
        tickets = rng.randint(40, 400)
        price = Decimal(rng.randrange(8000, 15001, 50))
        refunded_tickets = rng.choice([0, 0, 0, 1, 1, 2])
        if day == GAP_DAY:
            day += timedelta(days=1)  # no record at all
            continue
        if day == ZERO_SALES_DAY:
            rows.append(_row(day, 0, Decimal(0), Decimal(0)))
        elif day == NEGATIVE_NET_DAY:
            # Refunds of earlier days can exceed what was sold today.
            rows.append(_row(day, 3, Decimal("30000.00"), Decimal("45000.00")))
        else:
            rows.append(_row(day, tickets, tickets * price, min(refunded_tickets, tickets) * price))
        day += timedelta(days=1)
    rows.append(_row(PRESALE_DAY, 120, 120 * Decimal("12500.00"), Decimal(0)))
    return {"currency": "ARS", "days": rows}


def render() -> str:
    return json.dumps(build_document(), indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str]) -> int:
    target = Path(argv[1]) if len(argv) > 1 else OUTPUT
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render(), encoding="utf-8", newline="\n")
    sys.stdout.write(f"wrote {target}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
