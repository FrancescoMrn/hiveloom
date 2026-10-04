"""Deterministic provider for the routing-lab demo.

This keeps the build/run/evolve/fork walkthrough reproducible and offline. It
is deliberately scoped to this harness through ``extensions``; it is not a
runtime builtin and is not intended for production work.
"""

from __future__ import annotations

import json
import re
from typing import Any

from hiveloom.ext import ModelInfo
from hiveloom.models.fake import text_response, tool_response
from hiveloom.models.provider import Message, ModelConfig, ModelProvider, ModelResponse


def _contains(messages: list[Message], needle: str) -> bool:
    return needle in json.dumps(messages, sort_keys=True)


def _asked_to_plan(messages: list[Message]) -> bool:
    """plan_then_act's planning turn: the task asks for a plan, nothing answered yet."""
    return (
        len(messages) == 1
        and "output a brief numbered plan" in str(messages[0].get("content", ""))
    )


def _aimed_target(prompt: str) -> dict[str, Any]:
    """Aim at what the signal map in the evolve prompt measured.

    The failures in this demo are non-JSON final answers, which the map shows
    as the `verify_failed` status. If a run of this harness ever fails another
    way, the evolver falls back to the success rate rather than inventing a
    target the assessment could not check.
    """
    section = prompt.split("<signal_map>", 1)[-1].split("</signal_map>", 1)[0]
    match = re.search(r"^targets: (\[.*\])", section, re.MULTILINE)
    targets = json.loads(match.group(1)) if match else []
    if "status:verify_failed" in targets:
        return {"signal": "status:verify_failed", "expect": "decrease"}
    return {"signal": "success_rate", "expect": "increase"}


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
            prompt = str(messages[-1].get("content", "")) if messages else ""
            proposal = {
                "rationale": "Make the verified JSON contract explicit after repeated failures.",
                "target": _aimed_target(prompt),
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
            if _asked_to_plan(messages):
                return text_response(
                    "1. Read incident.txt for the symptoms and timeline.\n"
                    "2. Switch to the decide playbook once the evidence is in.\n"
                    "3. Commit to severity, owner and action as one JSON object."
                )
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
