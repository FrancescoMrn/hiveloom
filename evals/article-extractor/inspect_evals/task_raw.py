"""Raw arms: same system prompt, same fetch tool, no harness scaffolding.

inspect_ai's plain ``generate()`` tool loop is the zero-scaffolding baseline —
no validators, no retry-with-feedback, no guardrails. ``message_limit`` gives
rough parity with the harness's ``loop.max_turns: 10``.

The system prompt is inserted as a raw ChatMessageSystem rather than via
``system_message()``, whose template formatting would mangle the literal JSON
braces in the harness prompt.
"""

from __future__ import annotations

import time

from inspect_ai import Task, task
from inspect_ai.model import ChatMessageSystem
from inspect_ai.solver import Generate, Solver, TaskState, generate, solver, use_tools

from inspect_evals._fetch_tool import fetch_clean
from inspect_evals._shared import load_harness_system_prompt
from inspect_evals.scorer import article_extractor_scorer
from inspect_evals.task_harness import article_dataset


@solver
def harness_system_prompt() -> Solver:
    prompt = load_harness_system_prompt()

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        state.messages.insert(0, ChatMessageSystem(content=prompt))
        state.metadata["t_start"] = time.monotonic()
        return state

    return solve


@solver
def stop_timer() -> Solver:
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        state.metadata["latency_seconds"] = time.monotonic() - state.metadata.pop("t_start")
        return state

    return solve


@task
def article_extractor_raw():
    return Task(
        dataset=article_dataset(),
        solver=[harness_system_prompt(), use_tools(fetch_clean()), generate(), stop_timer()],
        scorer=article_extractor_scorer(),
        epochs=3,
        message_limit=24,
        time_limit=360,
    )
