"""Billing's check: the quote must be what the customer will be invoiced, in the
currency the request asked for. It reads the system of record, contracts
included — which the desk has no tool for."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_DATA = Path(__file__).resolve().parents[1] / "data"
_WEIGHT = re.compile(r"(\d+(?:\.\d+)?)\s*(kg|g)\b")
_ZONE = re.compile(r"zone (z\d)", re.I)
_CUSTOMER = re.compile(r"customer (C-\d+)")


def expected_price(request: str) -> float | None:
    weight, zone, customer = _WEIGHT.search(request), _ZONE.search(request), _CUSTOMER.search(
        request
    )
    if not (weight and zone and customer):
        return None
    kg = float(weight.group(1)) / (1000 if weight.group(2) == "g" else 1)
    table = json.loads((_DATA / "rates.json").read_text(encoding="utf-8"))
    contract = json.loads((_DATA / "customers.json").read_text(encoding="utf-8")).get(
        customer.group(1), {}
    )
    rate = table["zones"][zone.group(1).upper()]
    price = round(rate["base"] + rate["per_kg"] * kg, 2)
    if contract.get("contract"):
        price = round(price * (1 - contract["discount"]), 2)
    if "in EUR" in request:
        price = round(price * table["eur_per_usd"], 2)
    return price


def validate(run_output: str, run_context: dict[str, Any]) -> dict[str, Any]:
    try:
        answer = json.loads(run_output)
    except (json.JSONDecodeError, TypeError):
        return {"passed": False, "feedback": "Answer with one JSON object."}
    expected = expected_price(str(run_context.get("input", "")))
    price = answer.get("price") if isinstance(answer, dict) else None
    try:
        ok = expected is not None and price is not None and abs(float(price) - expected) <= 0.011
    except (TypeError, ValueError):
        ok = False
    if not ok:
        return {"passed": False, "feedback": "The quote does not match what billing will invoice."}
    return {"passed": True}
