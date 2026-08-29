"""A run is the only execution identity across journal and Hive."""

from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path

from hiveloom.logging.hive import Hive
from hiveloom.logging.trace import TraceWriter
from hiveloom.runner import run_harness


def test_runner_has_no_grouping_identity() -> None:
    assert "session_id" not in inspect.signature(run_harness).parameters


def test_trace_is_flat_and_carries_only_the_run_identity(tmp_path: Path) -> None:
    writer = TraceWriter(
        tmp_path,
        run_id="run_one",
        harness_name="demo",
        version_hash="abc123",
    )
    event = writer.emit("run_started", input="go")

    assert writer.path == tmp_path / "run_one.jsonl"
    assert "session_id" not in event.model_dump()


def test_fresh_hive_run_schema_has_no_grouping_column(tmp_path: Path) -> None:
    with Hive(tmp_path / "hive.db") as hive:
        columns = {
            row["name"] for row in hive._conn.execute("PRAGMA table_info(runs)")  # noqa: SLF001
        }

    assert "run_id" in columns
    assert "session_id" not in columns


def test_existing_hive_drops_the_obsolete_grouping_column(tmp_path: Path) -> None:
    path = tmp_path / "old-hive.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE runs (run_id TEXT PRIMARY KEY, harness_name TEXT, session_id TEXT)"
    )
    connection.execute("CREATE INDEX idx_runs_session ON runs(session_id)")
    connection.commit()
    connection.close()

    with Hive(path) as hive:
        columns = {
            row["name"] for row in hive._conn.execute("PRAGMA table_info(runs)")  # noqa: SLF001
        }

    assert "session_id" not in columns
