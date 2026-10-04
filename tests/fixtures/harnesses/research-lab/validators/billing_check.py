"""The billing system's check: the quote must be what the customer will be invoiced.

It reads the request itself, the rate table, and the customer contracts — the
system of record. The desk has no tool for contracts, so a contract customer's
quote at list price fails here, and no prompt can fix that: the desk would
need a contract lookup, a lever outside a prompt-only research charter.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_DATA = Path(__file__).resolve().parents[1] / "data"
_WEIGHT = re.compile(r"(\d+(?:\.\d+)?)\s*(kg|g)\b")
_ZONE = re.compile(r"zone (\d+)")
_CUSTOMER = re.compile(r"customer (C-\d+)")


def expected_price(request: str) -> float | None:
    weight, zone, customer = _WEIGHT.search(request), _ZONE.search(request), _CUSTOMER.search(
        request
    )
    if not (weight and zone and customer):
        return None
    kg = float(weight.group(1)) / (1000 if weight.group(2) == "g" else 1)
    rates = json.loads((_DATA / "rates.json").read_text(encoding="utf-8"))["zones"]
    contract = json.loads((_DATA / "customers.json").read_text(encoding="utf-8")).get(
        customer.group(1), {}
    )
    rate = rates[zone.group(1)]
    price = rate["base"] + rate["per_kg"] * kg
    if contract.get("contract"):
        price *= 1 - contract["discount"]
    return round(price, 2)


def validate(run_output: str, run_context: dict[str, Any]) -> dict[str, Any]:
    try:
        answer = json.loads(run_output)
    except (json.JSONDecodeError, TypeError):
        return {"passed": False, "feedback": "Answer with one JSON object."}
    expected = expected_price(str(run_context.get("input", "")))
    price = answer.get("price") if isinstance(answer, dict) else None
    if expected is None or price is None or abs(float(price) - expected) > 0.005:
        return {"passed": False, "feedback": "The quote does not match what billing will invoice."}
    return {"passed": True}
