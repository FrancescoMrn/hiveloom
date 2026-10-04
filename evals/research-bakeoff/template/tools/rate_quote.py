"""The carrier's rate service: a list price in USD by zone code and weight in kilograms."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hiveloom.tools import tool
from hiveloom.tools.registry import ToolError

_RATES = Path(__file__).resolve().parents[1] / "data" / "rates.json"
_CALLS: dict[tuple[str | None, str], int] = {}


@tool(
    description=(
        "The list price in USD for shipping one parcel: zone (a zone code such as Z1), "
        "weight_kg (the weight in kilograms) and customer (the customer id)."
    ),
    tags=["rates", "read"],
)
def rate_quote(
    zone: str, weight_kg: float, customer: str, run_context: dict[str, Any] | None = None
) -> str:
    table = json.loads(_RATES.read_text(encoding="utf-8"))
    # Some accounts sit on a slow partition: the first call of a run for them
    # is refused and a second one answers. Per run, so every run sees the same.
    if customer in table["busy_customers"]:
        key = ((run_context or {}).get("run_id"), customer)
        _CALLS[key] = _CALLS.get(key, 0) + 1
        if _CALLS[key] == 1:
            raise ToolError("rate service busy (503)")
    rate = table["zones"].get(zone)
    if rate is None:
        raise ToolError(f"no zone {zone!r} in the rate table")
    if weight_kg <= 0 or weight_kg > table["parcel_limit_kg"]:
        raise ToolError(
            f"weight {weight_kg:g} kg is outside the parcel limit (0-{table['parcel_limit_kg']} kg)"
        )
    price = round(rate["base"] + rate["per_kg"] * weight_kg, 2)
    return json.dumps({
        "zone": zone, "weight_kg": weight_kg, "list_price_usd": price,
        "eur_per_usd": table["eur_per_usd"],
    })
