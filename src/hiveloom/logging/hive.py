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
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# Validator feedback is written for the executor model to read, so it embeds the
# offending value: the title that was wrong, the headings that were fabricated,
# the line and column of a parse error. Grouping failures on that raw text makes
# a recurring behaviour fragment into one group per input. These patterns strip
# the interpolated parts back out, leaving the shape of the complaint.
#
# Deliberately generic — no pattern here knows about any particular validator or
# harness. It is a fallback for the free-text feedback contract that exists
# today; a validator emitting a stable diagnostic code would not need it.
# Order matters: quoted forms are collapsed before bare URLs, so a quoted URL
# becomes <str> rather than leaving an orphaned quote around <url>.
_PLACEHOLDERS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\[[^\[\]]*\]"), "<list>"),  # ['H2: ...', 'H2: ...']
    (re.compile(r"'[^']*'"), "<str>"),
    (re.compile(r'"[^"]*"'), "<str>"),
    (re.compile(r"https?://\S+"), "<url>"),
    (re.compile(r"\b\d+\b"), "<num>"),
)


def normalize_feedback(feedback: str) -> str:
    """Reduce validator feedback to a template that groups across inputs."""
    text = feedback
    for pattern, placeholder in _PLACEHOLDERS:
        text = pattern.sub(placeholder, text)
    return " ".join(text.split())


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
CREATE TABLE IF NOT EXISTS proposals (
    id TEXT PRIMARY KEY,
    harness_name TEXT,
    spec_version_hash TEXT,
    dedup_key TEXT,
    status TEXT,
    trigger TEXT,
    rationale TEXT,
    proposal_json TEXT,
    gate_json TEXT,
    apply_result_json TEXT,
    created_at TEXT,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_name ON runs(harness_name);
CREATE INDEX IF NOT EXISTS idx_verifications_run ON verifications(run_id);
CREATE INDEX IF NOT EXISTS idx_guardrail_run ON guardrail_triggers(run_id);
CREATE INDEX IF NOT EXISTS idx_evolutions_name ON evolutions(harness_name);
CREATE INDEX IF NOT EXISTS idx_proposals_name ON proposals(harness_name);
CREATE UNIQUE INDEX IF NOT EXISTS idx_proposals_dedup
    ON proposals(harness_name, spec_version_hash, dedup_key) WHERE status='pending';
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
        self._conn = sqlite3.connect(str(self.path), timeout=5)
        self._conn.row_factory = sqlite3.Row
        # WAL lets a running CLI ingest traces without blocking read-only stats
        # commands. The timeout handles brief write contention gracefully.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
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

    def failure_signatures(
        self, harness_name: str, limit: int = 10, *, version: str | None = None
    ) -> dict[str, Any]:
        """Most common failure verdicts, guardrail triggers, and statuses.

        Verdicts are grouped by a normalised template rather than by the raw
        feedback string. Validators interpolate the offending value into their
        feedback, so grouping on it verbatim splits one recurring behaviour
        into one group per input — exactly backwards, since a behaviour is
        systematic precisely when it recurs across *different* inputs.

        Counts are distinct runs, not verification rows: a run failing two
        validators must not count twice toward either.

        Pass ``version`` to restrict the analysis to one harness version, so
        failures an earlier evolution already repaired stop influencing the
        next proposal.
        """
        scope = "r.harness_name=?" + (" AND r.harness_version_hash=?" if version else "")
        args: tuple[Any, ...] = (harness_name, version) if version else (harness_name,)
        rows = self._conn.execute(
            "SELECT v.feedback AS feedback, r.run_id AS run_id "
            "FROM verifications v JOIN runs r ON v.run_id=r.run_id "
            f"WHERE {scope} AND v.passed=0 AND v.feedback != ''",
            args,
        ).fetchall()

        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            entry = grouped.setdefault(
                normalize_feedback(row["feedback"]),
                {"runs": set(), "example": row["feedback"]},
            )
            entry["runs"].add(row["run_id"])
        verdicts = sorted(
            (
                {"feedback": template, "count": len(e["runs"]), "example": e["example"]}
                for template, e in grouped.items()
            ),
            key=lambda e: e["count"],
            reverse=True,
        )[:limit]
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
            "verdicts": verdicts,
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

    def prune_runs(
        self, retention_days: int, *, now: datetime | None = None
    ) -> int:
        """Delete completed runs older than ``retention_days`` and their details.

        Retention is intentionally explicit: opening a Hive never silently
        removes historical run data. Callers may schedule this maintenance
        method according to their own data-retention policy.
        """
        if retention_days < 1:
            raise ValueError("retention_days must be at least 1")
        reference = now or datetime.now(UTC)
        if reference.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        cutoff = (reference.astimezone(UTC) - timedelta(days=retention_days)).isoformat()
        run_ids = [
            row["run_id"]
            for row in self._conn.execute(
                "SELECT run_id FROM runs "
                "WHERE finished_at IS NOT NULL AND finished_at < ?",
                (cutoff,),
            )
        ]
        if not run_ids:
            return 0
        placeholders = ", ".join("?" for _ in run_ids)
        self._conn.execute(f"DELETE FROM verifications WHERE run_id IN ({placeholders})", run_ids)
        self._conn.execute(
            f"DELETE FROM guardrail_triggers WHERE run_id IN ({placeholders})", run_ids
        )
        self._conn.execute(f"DELETE FROM runs WHERE run_id IN ({placeholders})", run_ids)
        self._conn.commit()
        return len(run_ids)

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

    def failure_count(self, harness_name: str, *, since: str | None = None) -> int:
        """COUNT(*) of non-success runs, optionally since an ISO timestamp."""
        query = "SELECT COUNT(*) AS n FROM runs WHERE harness_name=? AND status != 'success'"
        params: list[Any] = [harness_name]
        if since is not None:
            query += " AND finished_at >= ?"
            params.append(since)
        row = self._conn.execute(query, params).fetchone()
        return row["n"] or 0

    # ------------------------------------------------------------------ #
    # Proposals queue
    # ------------------------------------------------------------------ #
    def insert_proposal(self, row: dict[str, Any]) -> dict[str, Any]:
        """Insert a pending proposal row.

        The partial unique index on ``(harness_name, spec_version_hash,
        dedup_key) WHERE status='pending'`` is the dedup mechanism: if a
        pending proposal already occupies this slot, the collision is caught
        here and that existing row is returned instead of raising — dedup by
        construction, safe even under a race with another inserter.
        """
        try:
            self._conn.execute(
                "INSERT INTO proposals (id, harness_name, spec_version_hash, dedup_key, "
                "status, trigger, rationale, proposal_json, gate_json, apply_result_json, "
                "created_at, resolved_at) VALUES (:id, :harness_name, :spec_version_hash, "
                ":dedup_key, :status, :trigger, :rationale, :proposal_json, :gate_json, "
                ":apply_result_json, :created_at, :resolved_at)",
                row,
            )
            self._conn.commit()
            return dict(row)
        except sqlite3.IntegrityError:
            # The failed INSERT opened a transaction in sqlite3's default
            # non-autocommit mode. End it before the lookup so this connection
            # does not keep a needless write lock after the expected collision.
            self._conn.rollback()
            existing = self.find_pending_proposal(
                row["harness_name"], row["spec_version_hash"], row["dedup_key"]
            )
            if existing is None:
                raise
            return existing

    def find_pending_proposal(
        self, harness_name: str, spec_version_hash: str, dedup_key: str
    ) -> dict[str, Any] | None:
        """The pending proposal occupying this dedup slot, if any."""
        row = self._conn.execute(
            "SELECT * FROM proposals WHERE harness_name=? AND spec_version_hash=? "
            "AND dedup_key=? AND status='pending'",
            (harness_name, spec_version_hash, dedup_key),
        ).fetchone()
        return dict(row) if row else None

    def get_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        """Fetch a single proposal row by id."""
        row = self._conn.execute(
            "SELECT * FROM proposals WHERE id=?", (proposal_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_proposals(
        self, harness_name: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        """List proposals, optionally filtered by harness and/or status, newest first."""
        query = "SELECT * FROM proposals WHERE 1=1"
        params: list[Any] = []
        if harness_name is not None:
            query += " AND harness_name=?"
            params.append(harness_name)
        if status is not None:
            query += " AND status=?"
            params.append(status)
        query += " ORDER BY created_at DESC"
        rows = self._conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def last_auto_proposal_at(self, harness_name: str) -> str | None:
        """Creation timestamp of this harness's newest auto-triggered proposal."""
        row = self._conn.execute(
            "SELECT created_at FROM proposals "
            "WHERE harness_name=? AND trigger='auto' "
            "ORDER BY created_at DESC LIMIT 1",
            (harness_name,),
        ).fetchone()
        return row["created_at"] if row else None

    def claim_pending_proposal(self, proposal_id: str) -> bool:
        """Atomically move a pending proposal into the transient applying state."""
        cursor = self._conn.execute(
            "UPDATE proposals SET status='applying' WHERE id=? AND status='pending'",
            (proposal_id,),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    def release_proposal_claim(self, proposal_id: str) -> None:
        """Return a failed apply's claimed proposal to pending for a retry.

        Best-effort by design. The dedup index is partial on ``status='pending'``,
        so a claimed proposal's slot is momentarily free and a concurrent
        ``create_proposal`` can queue a fresh row for the same failure cluster.
        Restoring this one would then collide on that index — and since this
        runs inside the failed apply's exception handler, raising would mask the
        real error. The colliding row already represents the same work, so
        leaving this one claimed loses nothing.
        """
        try:
            self._conn.execute(
                "UPDATE proposals SET status='pending' WHERE id=? AND status='applying'",
                (proposal_id,),
            )
            self._conn.commit()
        except sqlite3.IntegrityError:
            self._conn.rollback()

    def update_proposal(self, proposal_id: str, **fields: Any) -> None:
        """Update selected columns of a proposal row (e.g. status, resolved_at)."""
        if not fields:
            return
        assignments = ", ".join(f"{key}=?" for key in fields)
        self._conn.execute(
            f"UPDATE proposals SET {assignments} WHERE id=?",
            (*fields.values(), proposal_id),
        )
        self._conn.commit()

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
