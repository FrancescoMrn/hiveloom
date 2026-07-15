"""The Hive: a queryable SQLite index over JSONL run traces.

The Hive is the memory that makes self-improvement possible. It ingests trace
JSONL from any directory (including in-folder traces copied back from a deployed
harness), idempotently by ``run_id``, and answers the questions the evolver
needs:

* success rate / cost / turn count per harness **per version hash**,
* most common failure verdicts and guardrail triggers,
* the N most recent failed runs with their verifier feedback.

Default database: ``~/.hiveloom/hive.db`` (override with ``$HIVELOOM_DB``).
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    harness_name TEXT,
    harness_version_hash TEXT,
    status TEXT,
    turns INTEGER,
    cost_usd REAL,
    duration_seconds REAL,
    started_at TEXT,
    finished_at TEXT,
    reason TEXT,
    trace_path TEXT
);
CREATE TABLE IF NOT EXISTS verifications (
    run_id TEXT,
    seq INTEGER,
    verifier TEXT,
    passed INTEGER,
    feedback TEXT
);
CREATE TABLE IF NOT EXISTS guardrail_triggers (
    run_id TEXT,
    seq INTEGER,
    guardrail TEXT,
    kind TEXT,
    reason TEXT,
    hook TEXT
);
CREATE TABLE IF NOT EXISTS evolutions (
    harness_name TEXT,
    old_version_hash TEXT,
    new_version_hash TEXT,
    counter INTEGER,
    rationale TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_name ON runs(harness_name);
CREATE INDEX IF NOT EXISTS idx_verifications_run ON verifications(run_id);
CREATE INDEX IF NOT EXISTS idx_guardrail_run ON guardrail_triggers(run_id);
CREATE INDEX IF NOT EXISTS idx_evolutions_name ON evolutions(harness_name);
"""


def default_db_path() -> Path:
    """The default Hive database path (``$HIVELOOM_DB`` or ``<hiveloom home>/hive.db``)."""
    override = os.environ.get("HIVELOOM_DB")
    if override:
        return Path(override)
    from hiveloom.paths import hiveloom_home

    return hiveloom_home() / "hive.db"


class Hive:
    """A queryable index over run traces, backed by SQLite."""

    def __init__(self, db_path: str | Path | None = None):
        self.path = Path(db_path) if db_path is not None else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Hive:
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Ingestion (idempotent by run_id)
    # ------------------------------------------------------------------ #
    def ingest_trace_file(self, path: str | Path) -> list[str]:
        """Ingest one JSONL trace file. Returns the run_ids ingested."""
        file_path = Path(path)
        events: list[dict[str, Any]] = []
        for line in file_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("run_id"):
                events.append(event)

        by_run: dict[str, list[dict[str, Any]]] = {}
        for event in events:
            by_run.setdefault(event["run_id"], []).append(event)

        ingested: list[str] = []
        for run_id, run_events in by_run.items():
            self._ingest_run(run_id, run_events, str(file_path))
            ingested.append(run_id)
        self._conn.commit()
        return ingested

    def ingest_dir(self, trace_dir: str | Path) -> int:
        """Ingest every ``*.jsonl`` run trace in a directory. Returns run count."""
        directory = Path(trace_dir)
        if not directory.exists():
            return 0
        count = 0
        for file_path in sorted(directory.glob("*.jsonl")):
            count += len(self.ingest_trace_file(file_path))
        return count

    def _ingest_run(self, run_id: str, events: list[dict[str, Any]], trace_path: str) -> None:
        envelope = events[0]
        row: dict[str, Any] = {
            "run_id": run_id,
            "harness_name": envelope.get("harness_name", ""),
            "harness_version_hash": envelope.get("harness_version_hash", ""),
            "status": "incomplete",
            "turns": 0,
            "cost_usd": 0.0,
            "duration_seconds": 0.0,
            "started_at": None,
            "finished_at": None,
            "reason": "",
            "trace_path": trace_path,
        }
        verifications: list[tuple] = []
        triggers: list[tuple] = []

        for event in events:
            etype = event.get("type")
            payload = event.get("payload", {})
            if etype == "run_started":
                row["started_at"] = event.get("timestamp")
            elif etype == "run_finished":
                row["status"] = payload.get("status", "incomplete")
                row["turns"] = payload.get("turns", 0)
                row["cost_usd"] = payload.get("cost_usd", 0.0)
                row["duration_seconds"] = payload.get("duration_seconds", 0.0)
                row["reason"] = payload.get("reason", "")
                row["finished_at"] = event.get("timestamp")
            elif etype == "verification_result":
                verifications.append(
                    (
                        run_id,
                        event.get("seq", 0),
                        payload.get("verifier", ""),
                        1 if payload.get("passed") else 0,
                        payload.get("feedback", ""),
                    )
                )
            elif etype == "guardrail_triggered":
                triggers.append(
                    (
                        run_id,
                        event.get("seq", 0),
                        payload.get("guardrail", ""),
                        payload.get("kind", ""),
                        payload.get("reason", ""),
                        payload.get("hook", ""),
                    )
                )

        cur = self._conn
        cur.execute("DELETE FROM runs WHERE run_id=?", (run_id,))
        cur.execute("DELETE FROM verifications WHERE run_id=?", (run_id,))
        cur.execute("DELETE FROM guardrail_triggers WHERE run_id=?", (run_id,))
        cur.execute(
            "INSERT INTO runs (run_id, harness_name, harness_version_hash, status, turns, "
            "cost_usd, duration_seconds, started_at, finished_at, reason, trace_path) "
            "VALUES (:run_id, :harness_name, :harness_version_hash, :status, :turns, "
            ":cost_usd, :duration_seconds, :started_at, :finished_at, :reason, :trace_path)",
            row,
        )
        cur.executemany(
            "INSERT INTO verifications (run_id, seq, verifier, passed, feedback) "
            "VALUES (?, ?, ?, ?, ?)",
            verifications,
        )
        cur.executemany(
            "INSERT INTO guardrail_triggers (run_id, seq, guardrail, kind, reason, hook) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            triggers,
        )

    # ------------------------------------------------------------------ #
    # Queries
    # ------------------------------------------------------------------ #
    def version_stats(self, harness_name: str) -> list[dict[str, Any]]:
        """Per-version-hash aggregates so evolution can be judged."""
        rows = self._conn.execute(
            "SELECT harness_version_hash AS version, "
            "COUNT(*) AS runs, "
            "SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS successes, "
            "AVG(cost_usd) AS avg_cost_usd, "
            "AVG(turns) AS avg_turns "
            "FROM runs WHERE harness_name=? GROUP BY harness_version_hash "
            "ORDER BY runs DESC",
            (harness_name,),
        ).fetchall()
        result = []
        for row in rows:
            runs = row["runs"] or 0
            successes = row["successes"] or 0
            result.append(
                {
                    "version": row["version"],
                    "runs": runs,
                    "successes": successes,
                    "success_rate": (successes / runs) if runs else 0.0,
                    "avg_cost_usd": row["avg_cost_usd"] or 0.0,
                    "avg_turns": row["avg_turns"] or 0.0,
                }
            )
        return result

    def failure_signatures(self, harness_name: str, limit: int = 10) -> dict[str, Any]:
        """Most common failure verdicts, guardrail triggers, and statuses."""
        verdicts = self._conn.execute(
            "SELECT v.feedback AS feedback, COUNT(*) AS count "
            "FROM verifications v JOIN runs r ON v.run_id=r.run_id "
            "WHERE r.harness_name=? AND v.passed=0 AND v.feedback != '' "
            "GROUP BY v.feedback ORDER BY count DESC LIMIT ?",
            (harness_name, limit),
        ).fetchall()
        guardrails = self._conn.execute(
            "SELECT g.guardrail AS guardrail, g.kind AS kind, COUNT(*) AS count "
            "FROM guardrail_triggers g JOIN runs r ON g.run_id=r.run_id "
            "WHERE r.harness_name=? GROUP BY g.guardrail, g.kind ORDER BY count DESC LIMIT ?",
            (harness_name, limit),
        ).fetchall()
        statuses = self._conn.execute(
            "SELECT status, COUNT(*) AS count FROM runs "
            "WHERE harness_name=? AND status != 'success' "
            "GROUP BY status ORDER BY count DESC",
            (harness_name,),
        ).fetchall()
        return {
            "verdicts": [dict(r) for r in verdicts],
            "guardrails": [dict(r) for r in guardrails],
            "statuses": [dict(r) for r in statuses],
        }

    def recent_failures(self, harness_name: str, n: int = 5) -> list[dict[str, Any]]:
        """The N most recent failed runs with their failing verifier feedback."""
        runs = self._conn.execute(
            "SELECT * FROM runs WHERE harness_name=? AND status != 'success' "
            "ORDER BY finished_at DESC LIMIT ?",
            (harness_name, n),
        ).fetchall()
        result = []
        for run in runs:
            feedback = self._conn.execute(
                "SELECT verifier, feedback FROM verifications "
                "WHERE run_id=? AND passed=0 AND feedback != ''",
                (run["run_id"],),
            ).fetchall()
            triggers = self._conn.execute(
                "SELECT guardrail, kind, reason FROM guardrail_triggers WHERE run_id=?",
                (run["run_id"],),
            ).fetchall()
            entry = dict(run)
            entry["failed_verifications"] = [dict(f) for f in feedback]
            entry["guardrail_triggers"] = [dict(t) for t in triggers]
            result.append(entry)
        return result

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        """Fetch a single run with its verifications and guardrail triggers."""
        run = self._conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if run is None:
            return None
        entry = dict(run)
        entry["verifications"] = [
            dict(r)
            for r in self._conn.execute(
                "SELECT seq, verifier, passed, feedback FROM verifications WHERE run_id=? "
                "ORDER BY seq",
                (run_id,),
            ).fetchall()
        ]
        entry["guardrail_triggers"] = [
            dict(r)
            for r in self._conn.execute(
                "SELECT seq, guardrail, kind, reason, hook FROM guardrail_triggers "
                "WHERE run_id=? ORDER BY seq",
                (run_id,),
            ).fetchall()
        ]
        return entry

    def record_evolution(
        self,
        harness_name: str,
        old_version_hash: str,
        new_version_hash: str,
        counter: int,
        rationale: str,
        created_at: str,
    ) -> None:
        """Record an evolution (old/new version hashes + rationale)."""
        self._conn.execute(
            "INSERT INTO evolutions (harness_name, old_version_hash, new_version_hash, "
            "counter, rationale, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (harness_name, old_version_hash, new_version_hash, counter, rationale, created_at),
        )
        self._conn.commit()

    def evolutions(self, harness_name: str) -> list[dict[str, Any]]:
        """Return the recorded evolutions for a harness, newest first."""
        rows = self._conn.execute(
            "SELECT * FROM evolutions WHERE harness_name=? ORDER BY created_at DESC",
            (harness_name,),
        ).fetchall()
        return [dict(r) for r in rows]

    def summary(self, harness_name: str) -> dict[str, Any]:
        """A rolled-up stats view for one harness (all versions + per version)."""
        totals = self._conn.execute(
            "SELECT COUNT(*) AS runs, "
            "SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS successes, "
            "AVG(cost_usd) AS avg_cost_usd, AVG(turns) AS avg_turns "
            "FROM runs WHERE harness_name=?",
            (harness_name,),
        ).fetchone()
        runs = totals["runs"] or 0
        successes = totals["successes"] or 0
        return {
            "harness_name": harness_name,
            "total_runs": runs,
            "success_rate": (successes / runs) if runs else 0.0,
            "avg_cost_usd": totals["avg_cost_usd"] or 0.0,
            "avg_turns": totals["avg_turns"] or 0.0,
            "versions": self.version_stats(harness_name),
            "failure_signatures": self.failure_signatures(harness_name),
        }
