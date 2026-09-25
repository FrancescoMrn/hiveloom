"""Deterministic provider for ledger-desk, the delegation-lab specialist.

A scripted clerk: it finds the invoice id in its task, looks it up, and answers
from the tool result it received. Offline and reproducible; not a builtin.
"""

from __future__ import annotations

import json
import re
from typing import Any

from hiveloom.ext import ModelInfo
from hiveloom.models.fake import text_response, tool_response
from hiveloom.models.provider import Message, ModelConfig, ModelProvider, ModelResponse

_INVOICE = re.compile(r"\binv-\d{4}\b", re.IGNORECASE)


def _task(messages: list[Message]) -> str:
    for message in messages:
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            return message["content"]
    return ""


def _result(messages: list[Message]) -> tuple[str, bool] | None:
    for message in messages:
        for block in message.get("content") if isinstance(message.get("content"), list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                return str(block.get("content", "")), bool(block.get("is_error"))
    return None


class LedgerProvider(ModelProvider):
    def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[dict[str, Any]],
        config: ModelConfig,
    ) -> ModelResponse:
        match = _INVOICE.search(_task(messages))
        if match is None:
            return text_response(json.dumps({"answer": "No invoice id in the request."}))
        # The specialist knows its ledger: ids are stored in uppercase.
        invoice_id = match.group(0).upper()
        result = _result(messages)
        if result is None:
            return tool_response("lookup_invoice", {"invoice_id": invoice_id}, call_id="lookup")
        body, is_error = result
        if is_error:
            return text_response(json.dumps({"answer": f"{invoice_id} is not in the ledger."}))
        row = json.loads(body)
        return text_response(json.dumps({
            "answer": (
                f"Invoice {row['invoice_id']} for {row['customer']} is "
                f"{row['amount']:.2f} {row['currency']}."
            ),
        }))


def hiveloom_extension(hive) -> None:
    hive.register_provider(
        "ledger_desk",
        lambda _ctx: LedgerProvider(),
        models=[ModelInfo(id="clerk", provider="ledger_desk", context_window=32768)],
        api="local",
        open_catalog=False,
        label="Ledger desk (offline demo)",
    )
