"""Solver that runs a sample through the hiveloom CLI."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time

from inspect_ai.solver import Generate, Solver, TaskState, solver

from inspect_evals._shared import EVAL_ROOT, REPO_ROOT

SUBPROCESS_TIMEOUT = 300  # hard backstop over the harness's own 240s wall clock

# The --json contract needs plain bytes; color-forcing vars (e.g. FORCE_COLOR
# exported by CI/agent shells) would make rich inject ANSI into stdout.
_CHILD_ENV = {
    k: v for k, v in os.environ.items() if k not in ("FORCE_COLOR", "CLICOLOR_FORCE")
} | {"NO_COLOR": "1"}


def _hiveloom_cmd() -> list[str]:
    env_bin = os.environ.get("HIVELOOM_BIN")
    if env_bin:
        return [env_bin]
    venv_bin = REPO_ROOT / ".venv" / "bin" / "hiveloom"
    if venv_bin.exists():
        return [str(venv_bin)]
    return ["hiveloom"]


@solver
def hiveloom_subprocess(harness_dir: str) -> Solver:
    resolved_dir = str((EVAL_ROOT / harness_dir).resolve())
    cmd_base = _hiveloom_cmd()

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        url = state.input_text
        start = time.monotonic()
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                [*cmd_base, "run", resolved_dir, "--input", url, "--json", "--approve"],
                capture_output=True,
                text=True,
                timeout=SUBPROCESS_TIMEOUT,
                check=False,
                env=_CHILD_ENV,
            )
        except subprocess.TimeoutExpired:
            result = {
                "ok": False,
                "status": "external_timeout",
                "output": None,
                "cost_usd": None,
                "reason": f"subprocess exceeded {SUBPROCESS_TIMEOUT}s",
            }
        else:
            try:
                result = json.loads(proc.stdout)
            except json.JSONDecodeError:
                # Exit codes 1 (verify_failed) and 2 (guardrail_halt) still emit
                # JSON — non-JSON stdout means something genuinely broke.
                result = {
                    "ok": False,
                    "status": "runtime_error",
                    "output": None,
                    "cost_usd": None,
                    "reason": (
                        f"non-JSON stdout, returncode={proc.returncode}: "
                        f"{(proc.stderr or proc.stdout)[:500]}"
                    ),
                }

        state.metadata["hiveloom_result"] = result
        state.metadata["latency_seconds"] = time.monotonic() - start
        state.output.completion = result.get("output") or ""
        return state

    return solve
