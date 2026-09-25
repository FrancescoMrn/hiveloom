"""The per-run cap on `propose_memory`, with the calls actually running at once.

`loop.tool_execution: parallel` runs a turn's tool calls together, and the cap
is a check-then-act against one set they share. The sequential cases live in
`test_propose_memory.py`; this file is the one that runs them concurrently.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from hiveloom import construct
from hiveloom.evolve.proposals import list_proposals
from hiveloom.logging.hive import Hive
from hiveloom.spec.loader import load_spec
from hiveloom.tools.builtin import ProposeMemoryTool

RUN_ID = "run_parallel"


def _harness(tmp_path: Path, **params: object) -> Path:
    directory = tmp_path / "proposer"
    construct.init_harness(directory, name="proposer", task="Summarize the input.")
    construct.set_field(directory, "loop.require_verification", "false")
    construct.add_tool(directory, builtin="propose_memory", **params)
    return directory


def _bound(harness: Path, *, max_per_run: int) -> ProposeMemoryTool:
    """The tool as the loop hands it over: a spec, this run's redaction, a journal."""
    tool = ProposeMemoryTool(max_per_run=max_per_run)
    tool.bind(
        load_spec(harness),
        redact=lambda value: value,
        journal=lambda _event, **_payload: None,
    )
    return tool


def _ready_queue() -> None:
    """Open the Hive once before the barrier.

    A real run has already written to it by the time the executor proposes
    anything; creating the database — schema and WAL mode — from eight threads
    at once is a race in sqlite, not in the cap this file is about.
    """
    with Hive():
        pass


def _pending(harness: Path) -> list:
    with Hive() as hive:
        return list_proposals(hive, load_spec(harness).identity, status="pending")


def _propose_together(harness: Path, tool: ProposeMemoryTool, callers: int) -> list[str]:
    """Fire ``callers`` distinct lessons at the tool from a barrier."""
    _ready_queue()
    start = threading.Barrier(callers, timeout=10)

    def propose(index: int) -> str:
        start.wait()
        return tool.run(
            kind="rule",
            title=f"Lesson {index}",
            # Distinct per call: the queue dedups on content, which would
            # otherwise hold the count down for a reason that is not the cap.
            content=f"Lesson number {index}: nothing else in this run says this.",
            evidence=f"turn {index} of this run",
            run_context={"run_id": RUN_ID, "harness_dir": str(harness)},
        )

    with ThreadPoolExecutor(max_workers=callers) as pool:
        return list(pool.map(propose, range(callers)))


def test_parallel_calls_cannot_queue_past_the_per_run_cap(tmp_path: Path):
    harness = _harness(tmp_path, max_per_run=2)
    tool = _bound(harness, max_per_run=2)

    answers = _propose_together(harness, tool, callers=8)

    queued = [answer for answer in answers if answer.startswith("queued as proposal")]
    assert len(queued) == 2
    # What the receipts say is what a reviewer will find waiting.
    assert len(_pending(harness)) == 2
    refused = [answer for answer in answers if "its limit of 2" in answer]
    assert len(refused) == 6


def test_a_cap_of_one_admits_exactly_one_of_a_parallel_turn(tmp_path: Path):
    harness = _harness(tmp_path, max_per_run=1)
    tool = _bound(harness, max_per_run=1)

    answers = _propose_together(harness, tool, callers=6)

    assert sum(answer.startswith("queued as proposal") for answer in answers) == 1
    assert len(_pending(harness)) == 1
