"""The carrier's rate table: a price for one parcel by zone and weight in kilograms."""

from __future__ import annotations

import json
from pathlib import Path

from hiveloom.tools import tool
from hiveloom.tools.registry import ToolError

_RATES = Path(__file__).resolve().parents[1] / "data" / "rates.json"


@tool(
    description=(
        "The list price for shipping one parcel: zone (1-4) and weight_kg, the parcel's "
        "weight in kilograms."
    ),
    tags=["rates", "read", "deterministic"],
)
def rate_quote(zone: int, weight_kg: float) -> str:
    table = json.loads(_RATES.read_text(encoding="utf-8"))
    rate = table["zones"].get(str(zone))
    if rate is None:
        raise ToolError(f"no zone {zone} in the rate table")
    if weight_kg <= 0 or weight_kg > table["parcel_limit_kg"]:
        # Says only what happened: a gram figure read as kilograms lands here,
        # and the harness has to work out why from its own runs.
        raise ToolError(
            f"weight {weight_kg:g} kg is outside the parcel limit "
            f"(0-{table['parcel_limit_kg']} kg)"
        )
    price = round(rate["base"] + rate["per_kg"] * weight_kg, 2)
    return json.dumps({"zone": zone, "weight_kg": weight_kg, "list_price": price})
