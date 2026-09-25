"""Exact-match invoice lookup over the demo ledger."""

from __future__ import annotations

import json
from pathlib import Path

from hiveloom.tools import tool
from hiveloom.tools.registry import ToolError

_LEDGER = Path(__file__).resolve().parents[1] / "data" / "invoices.json"


@tool(
    description=(
        "Look up one invoice in the ledger by its id and return its customer, amount "
        "and currency. The match is exact."
    ),
    tags=["ledger", "read", "deterministic"],
)
def lookup_invoice(invoice_id: str) -> str:
    """Return the ledger row for ``invoice_id`` as JSON, or fail if there is none."""
    ledger = json.loads(_LEDGER.read_text(encoding="utf-8"))
    row = ledger.get(invoice_id)
    if row is None:
        # The ledger stores ids in uppercase and matches exactly; the message
        # says only what happened, so the lesson has to be learned.
        raise ToolError(f"no invoice with id {invoice_id!r} in the ledger")
    return json.dumps({"invoice_id": invoice_id, **row})
