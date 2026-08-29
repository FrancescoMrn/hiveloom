"""Deterministic provider for the routing-lab demo.

This keeps the build/run/evolve/fork walkthrough reproducible and offline. It
is deliberately scoped to this harness through ``extensions``; it is not a
runtime builtin and is not intended for production work.
"""

from __future__ import annotations

import json
from typing import Any

from hiveloom.ext import ModelInfo
from hiveloom.models.fake import text_response, tool_response
from hiveloom.models.provider import Message, ModelConfig, ModelProvider, ModelResponse


def _contains(messages: list[Message], needle: str) -> bool:
    return needle in json.dumps(messages, sort_keys=True)


class RoutingLabProvider(ModelProvider):
    """Small scripted state machine selected by the current model id."""

    def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[dict[str, Any]],
        config: ModelConfig,
    ) -> ModelResponse:
        if config.id == "qa-evolver":
            proposal = {
                "rationale": "Make the verified JSON contract explicit after repeated failures.",
                "yaml_changes": [
                    {
                        "path": "system_prompt",
                        "value": (
                            "ALWAYS_EMIT_JSON. Read the incident, use the triage and decision "
                            "playbooks, and finish with exactly one JSON object containing "
                            "severity, owner, and action."
                        ),
                        "rationale": "The failures were non-JSON final answers.",
                    }
                ],
                "code_changes": [],
            }
            return text_response(json.dumps(proposal))

        if config.id == "qa-alt":
            return text_response(
                json.dumps(
                    {
                        "severity": "high",
                        "owner": "platform-alt",
                        "action": "isolate and compare the fork",
                    }
                )
            )

        if config.id == "qa-triage":
            if not _contains(messages, '"type": "tool_result"'):
                return tool_response("file_read", {"path": "incident.txt"}, call_id="read-1")
            return tool_response(
                "switch_playbook",
                {"name": "decide", "reason": "evidence collected"},
                call_id="route-1",
            )

        if config.id == "qa-decision":
            if _contains(messages, "FORCE_FAIL") and "ALWAYS_EMIT_JSON" not in system:
                return text_response("I cannot decide yet.")
            return text_response(
                json.dumps(
                    {
                        "severity": "medium",
                        "owner": "platform",
                        "action": "restart the worker and inspect queue depth",
                    }
                )
            )

        return text_response("unsupported routing-lab model")


def hiveloom_extension(hive) -> None:
    models = [
        ModelInfo(id=model, provider="routing_lab", context_window=8192)
        for model in ("qa-triage", "qa-decision", "qa-alt", "qa-evolver")
    ]
    hive.register_provider(
        "routing_lab",
        lambda _ctx: RoutingLabProvider(),
        models=models,
        api="local",
        open_catalog=False,
        label="Routing Lab (offline demo)",
    )
