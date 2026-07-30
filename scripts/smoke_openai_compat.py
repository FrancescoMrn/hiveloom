#!/usr/bin/env python3
"""Live smoke test for OpenAICompatProvider against a real server.

Not a unit test — NOT under tests/, never imported by pytest, never run in
CI. Hard-gated behind ``HIVELOOM_LIVE_SMOKE=1`` so it can't fire by accident
(a bare invocation just prints a message and exits 0).

Sends three calls against a real OpenAI-compatible endpoint (OpenRouter,
Groq, Together, vLLM, Ollama, mlx_lm.server, ...):

  1. A plain completion, no tools.
  2. A tool-call-inducing completion with one trivial tool schema.
  3. A second turn that feeds the tool result back in, exercising the exact
     history round-trip that GitHub issue #5 broke (assistant turn with no
     text re-serializing as ``content: null``).

Usage::

    HIVELOOM_LIVE_SMOKE=1 uv run python scripts/smoke_openai_compat.py \\
        --base-url https://openrouter.ai/api/v1 \\
        --api-key-env OPENROUTER_API_KEY \\
        --model deepseek/deepseek-r1
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from hiveloom.models.openai_compat import OpenAICompatProvider
from hiveloom.models.provider import ModelConfig

_TOOL_SCHEMA = {
    "name": "get_weather",
    "description": "Look up the current weather for a city.",
    "input_schema": {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    },
}


def main() -> int:
    if os.environ.get("HIVELOOM_LIVE_SMOKE") != "1":
        print("skipped: set HIVELOOM_LIVE_SMOKE=1 to run this live smoke test")
        return 0

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="e.g. https://openrouter.ai/api/v1")
    parser.add_argument("--api-key-env", required=True, help="env var holding the API key")
    parser.add_argument("--model", required=True, help="model id as the endpoint expects it")
    args = parser.parse_args()

    api_key = os.environ.get(args.api_key_env)
    if not api_key:
        print(f"error: env var {args.api_key_env} is not set", file=sys.stderr)
        return 1

    provider = OpenAICompatProvider(args.base_url, api_key=api_key)
    config = ModelConfig(id=args.model)
    results: dict[str, object] = {}

    plain = provider.complete(
        system="You are a terse assistant.",
        messages=[{"role": "user", "content": "Say hello in exactly one word."}],
        tools=[],
        config=config,
    )
    results["plain_completion"] = plain.model_dump()

    tool_turn = provider.complete(
        system="You must use the get_weather tool to answer.",
        messages=[{"role": "user", "content": "What's the weather in Rome?"}],
        tools=[_TOOL_SCHEMA],
        config=config,
    )
    results["tool_call_completion"] = tool_turn.model_dump()

    from hiveloom.loop.agent_loop import AgentLoop

    history = [
        {"role": "user", "content": "What's the weather in Rome?"},
        {"role": "assistant", "content": AgentLoop.assistant_blocks(tool_turn)},
    ]
    if tool_turn.tool_calls:
        history.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_turn.tool_calls[0].id,
                        "content": "18C, partly cloudy",
                        "is_error": False,
                    }
                ],
            }
        )
    follow_up = provider.complete(
        system="You must use the get_weather tool to answer.",
        messages=history,
        tools=[_TOOL_SCHEMA],
        config=config,
    )
    results["follow_up_completion"] = follow_up.model_dump()

    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
