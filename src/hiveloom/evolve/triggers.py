"""Evaluate bounded, restart-safe auto-proposal triggers from Hive indexes."""

from __future__ import annotations

from typing import Any

from hiveloom.logging.hive import Hive
from hiveloom.spec.schema import AutoProposeTrigger


def _window_receipt(
    runs: list[dict[str, Any]], *, configured: dict[str, Any]
) -> dict[str, Any]:
    chronological = list(reversed(runs))
    return {
        "kind": configured["kind"],
        "configured": configured,
        "window_run_ids": [run["run_id"] for run in runs],
        "window_started_at": (
            chronological[0].get("finished_at") if chronological else None
        ),
        "window_ended_at": runs[0].get("finished_at") if runs else None,
        "window_runs": len(runs),
    }


def match_auto_trigger(
    hive: Hive,
    *,
    harness_key: str,
    version: str,
    current_run_id: str,
    current_status: str,
    triggers: list[AutoProposeTrigger],
) -> dict[str, Any] | None:
    """Return the first declared trigger matched by the current run.

    Trigger order is policy order. Every match includes the exact recent-run
    window used to make the decision so proposal review does not have to infer
    it from the current contents of a mutable Hive.
    """
    windows: dict[int, list[dict[str, Any]]] = {}

    for trigger in triggers:
        if trigger.window not in windows:
            windows[trigger.window] = hive.recent_run_window(
                harness_key,
                version=version,
                limit=trigger.window,
            )
        runs = windows[trigger.window]
        current = next(
            (run for run in runs if run["run_id"] == current_run_id),
            None,
        )
        if current is None:
            continue
        configured = trigger.model_dump(mode="json", exclude_none=True)
        receipt = _window_receipt(runs, configured=configured)

        if trigger.kind == "final_failure":
            if current_status == "success":
                continue
            failures = [run for run in runs if run["status"] != "success"]
            if len(failures) < trigger.minimum_runs:
                continue
            receipt["matched"] = {
                "runs": len(failures),
                "run_ids": [run["run_id"] for run in failures],
            }
            return receipt

        pattern = hive.repeated_friction_pattern(
            run_ids=[run["run_id"] for run in runs],
            current_run_id=current_run_id,
            category=trigger.category,
            minimum_runs=trigger.minimum_runs,
            recovered=trigger.recovered,
        )
        if pattern is None:
            continue
        receipt["matched"] = pattern
        return receipt

    return None
