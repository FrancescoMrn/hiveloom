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

import hashlib
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


# How much of a run's task statement the index keeps: enough to title and
# search a run, not enough to become a shadow copy of the journal.
_TASK_CHARS = 2000
_FRICTION_SUMMARY_CHARS = 500

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    harness_name TEXT,
    harness_id TEXT,
    harness_key TEXT,
    harness_version_hash TEXT,
    status TEXT,
    turns INTEGER,
    cost_usd REAL,
    duration_seconds REAL,
    started_at TEXT,
    finished_at TEXT,
    reason TEXT,
    trace_path TEXT,
    parent_run_id TEXT,
    forked_at_seq INTEGER,
    model_path TEXT,
    task TEXT
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
CREATE TABLE IF NOT EXISTS playbook_visits (
    run_id TEXT,
    seq INTEGER,
    playbook TEXT,
    entered_from TEXT,
    ok INTEGER,
    reason TEXT
);
CREATE TABLE IF NOT EXISTS run_outcomes (
    run_id TEXT PRIMARY KEY,
    outcome TEXT,
    source TEXT,
    detail TEXT,
    recorded_at TEXT
);
CREATE TABLE IF NOT EXISTS friction_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    category TEXT NOT NULL,
    phase TEXT,
    attempt INTEGER,
    component TEXT,
    fingerprint TEXT NOT NULL,
    recovered INTEGER NOT NULL,
    timestamp TEXT,
    summary TEXT NOT NULL,
    UNIQUE(run_id, seq, category, component)
);
CREATE TABLE IF NOT EXISTS run_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL,
    name TEXT NOT NULL,
    value REAL NOT NULL,
    direction TEXT NOT NULL,
    unit TEXT NOT NULL,
    source TEXT NOT NULL,
    scope TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eval_runs (
    eval_run_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    eval_id TEXT NOT NULL,
    harness_key TEXT NOT NULL,
    harness_behavior_hash TEXT NOT NULL,
    requested_provider TEXT NOT NULL,
    requested_model TEXT NOT NULL,
    repetitions INTEGER NOT NULL,
    manifest_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eval_cells (
    eval_run_id TEXT NOT NULL,
    cell_id TEXT NOT NULL,
    case_key TEXT NOT NULL,
    repetition INTEGER NOT NULL,
    status TEXT NOT NULL,
    run_id TEXT NOT NULL,
    run_status TEXT NOT NULL,
    scorer_status TEXT NOT NULL,
    requested_provider TEXT NOT NULL,
    requested_model TEXT NOT NULL,
    effective_provider TEXT,
    effective_model TEXT,
    execution_fingerprint TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    cost_source TEXT NOT NULL,
    verification_attempts INTEGER NOT NULL,
    first_pass_valid INTEGER,
    recovery_attempted INTEGER NOT NULL,
    recovered INTEGER NOT NULL,
    verification_final_status TEXT NOT NULL,
    trace_disabled INTEGER NOT NULL,
    finished_at TEXT,
    PRIMARY KEY (eval_run_id, cell_id)
);
CREATE INDEX IF NOT EXISTS idx_runs_name ON runs(harness_name);
CREATE INDEX IF NOT EXISTS idx_verifications_run ON verifications(run_id);
CREATE INDEX IF NOT EXISTS idx_guardrail_run ON guardrail_triggers(run_id);
CREATE INDEX IF NOT EXISTS idx_playbook_visits_run ON playbook_visits(run_id);
CREATE INDEX IF NOT EXISTS idx_playbook_visits_name ON playbook_visits(playbook);
CREATE INDEX IF NOT EXISTS idx_run_metrics_run ON run_metrics(run_id);
CREATE INDEX IF NOT EXISTS idx_run_metrics_name ON run_metrics(name);
CREATE INDEX IF NOT EXISTS idx_run_metrics_source ON run_metrics(source);
CREATE INDEX IF NOT EXISTS idx_eval_cells_run ON eval_cells(eval_run_id);
CREATE INDEX IF NOT EXISTS idx_eval_cells_result ON eval_cells(run_id);
CREATE INDEX IF NOT EXISTS idx_eval_cells_pair ON eval_cells(eval_run_id, case_key, repetition);
CREATE INDEX IF NOT EXISTS idx_evolutions_name ON evolutions(harness_name);
CREATE INDEX IF NOT EXISTS idx_proposals_name ON proposals(harness_name);
CREATE INDEX IF NOT EXISTS idx_friction_run ON friction_events(run_id);
CREATE INDEX IF NOT EXISTS idx_friction_category ON friction_events(category);
CREATE INDEX IF NOT EXISTS idx_friction_fingerprint ON friction_events(fingerprint);
CREATE UNIQUE INDEX IF NOT EXISTS idx_proposals_dedup
    ON proposals(harness_name, spec_version_hash, dedup_key) WHERE status='pending';
"""


def _friction_row(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["recovered"] = bool(result["recovered"])
    return result


def _friction_filters(
    harness_key: str,
    *,
    category: str | None,
    component: str | None,
    recovered: bool | None,
    model: str | None,
    since: str | None,
    until: str | None,
) -> tuple[list[str], list[Any]]:
    where = ["r.harness_key=?"]
    params: list[Any] = [harness_key]
    if category is not None:
        where.append("f.category=?")
        params.append(category)
    if component is not None:
        where.append("f.component=?")
        params.append(component)
    if recovered is not None:
        where.append("f.recovered=?")
        params.append(1 if recovered else 0)
    if model is not None:
        where.append("(r.effective_model=? OR r.requested_model=? OR r.model_path=?)")
        params.extend((model, model, model))
    if since is not None:
        where.append("f.timestamp>=?")
        params.append(since)
    if until is not None:
        where.append("f.timestamp<=?")
        params.append(until)
    return where, params


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
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created.

        The schema is `CREATE TABLE IF NOT EXISTS`, so a Hive from an earlier
        version keeps its original `runs` shape and would fail on newer
        columns. The Hive is a derived index that can always be rebuilt by
        re-ingesting, so obsolete pre-1.0 columns may also be removed here.
        """
        existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(runs)")}
        if "session_id" in existing:
            # The Hive is a derived index, so discard the obsolete grouping
            # column rather than preserving a shape no public surface uses.
            self._conn.execute("DROP INDEX IF EXISTS idx_runs_session")
            self._alter_runs("DROP COLUMN session_id", benign_error="no such column")
            existing.remove("session_id")
        for column, decl in (
            ("parent_run_id", "TEXT"),
            ("forked_at_seq", "INTEGER"),
            ("model_path", "TEXT"),
            ("task", "TEXT"),
            ("harness_id", "TEXT"),
            ("harness_key", "TEXT"),
            ("requested_provider", "TEXT"),
            ("requested_model", "TEXT"),
            ("effective_provider", "TEXT"),
            ("effective_model", "TEXT"),
            ("execution_fingerprint", "TEXT"),
        ):
            if column not in existing:
                self._alter_runs(
                    f"ADD COLUMN {column} {decl}", benign_error="duplicate column name"
                )
        # Rows ingested before harness identity existed key by name — the
        # pre-1.0 behaviour those rows were recorded under. Re-ingesting a
        # trace whose envelope carries an id upgrades its row in place.
        self._conn.execute(
            "UPDATE runs SET harness_key=harness_name WHERE harness_key IS NULL"
        )
        # Indexes over migrated columns belong here, not in _SCHEMA: on an
        # existing database the schema script runs before the ALTER, so an
        # index naming a new column would fail before it could be added.
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_runs_parent ON runs(parent_run_id)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_runs_key ON runs(harness_key)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_runs_effective_model ON runs(effective_model)"
        )

    def _alter_runs(self, clause: str, *, benign_error: str) -> None:
        """Apply one migration step, tolerating a concurrent connection winning it.

        Connections race the inspect-then-alter sequence whenever two are
        opened at once (the eval runner scores cells from worker threads); the
        loser's ALTER fails with an already-applied error that is safe to
        swallow because the winner left the schema in the intended shape.
        """
        try:
            self._conn.execute(f"ALTER TABLE runs {clause}")
        except sqlite3.OperationalError as exc:
            if benign_error not in str(exc):
                raise

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Hive:
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Ingestion (idempotent by run_id)
    # ------------------------------------------------------------------ #

    def upsert_eval_manifest(self, manifest: dict[str, Any], path: str) -> None:
        """Index one complete manifest snapshot transactionally for reporting."""
        try:
            self._upsert_eval_manifest(manifest, path)
        except Exception:
            self._conn.rollback()
            raise
        self._conn.commit()

    def _upsert_eval_manifest(self, manifest: dict[str, Any], path: str) -> None:
        identity = manifest.get("eval_identity") or {}
        self._conn.execute(
            "INSERT INTO eval_runs (eval_run_id, status, eval_id, harness_key, "
            "harness_behavior_hash, requested_provider, requested_model, repetitions, "
            "manifest_path, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(eval_run_id) DO UPDATE SET status=excluded.status, "
            "eval_id=excluded.eval_id, harness_key=excluded.harness_key, "
            "harness_behavior_hash=excluded.harness_behavior_hash, "
            "requested_provider=excluded.requested_provider, "
            "requested_model=excluded.requested_model, repetitions=excluded.repetitions, "
            "manifest_path=excluded.manifest_path, updated_at=excluded.updated_at",
            (
                manifest["eval_run_id"],
                manifest["status"],
                identity.get("eval_id", ""),
                manifest.get("harness_id", ""),
                manifest.get("harness_behavior_hash", ""),
                manifest.get("requested_provider", ""),
                manifest.get("requested_model", ""),
                int(manifest.get("repetitions", 1)),
                path,
                manifest.get("created_at", ""),
                manifest.get("updated_at", ""),
            ),
        )
        self._conn.execute(
            "DELETE FROM eval_cells WHERE eval_run_id=?", (manifest["eval_run_id"],)
        )
        rows = []
        for cell in manifest.get("cells") or []:
            verification = cell.get("verification") or {}
            first_pass = verification.get("first_pass_valid")
            rows.append(
                (
                    manifest["eval_run_id"],
                    cell["cell_id"],
                    cell["case_key"],
                    int(cell["repetition"]),
                    cell.get("status", "pending"),
                    cell.get("run_id", ""),
                    cell.get("run_status", ""),
                    cell.get("scorer_status", "not_run"),
                    cell.get("requested_provider", ""),
                    cell.get("requested_model", ""),
                    cell.get("effective_provider"),
                    cell.get("effective_model"),
                    cell.get("execution_fingerprint", ""),
                    int(cell.get("duration_ms", 0)),
                    float(cell.get("cost_usd", 0.0)),
                    cell.get("cost_source", "none"),
                    int(verification.get("attempts", 0)),
                    None if first_pass is None else (1 if first_pass else 0),
                    1 if verification.get("recovery_attempted") else 0,
                    1 if verification.get("recovered") else 0,
                    verification.get("final_status", "not_run"),
                    1 if cell.get("trace_disabled") else 0,
                    cell.get("finished_at"),
                )
            )
        self._conn.executemany(
            "INSERT INTO eval_cells (eval_run_id, cell_id, case_key, repetition, status, "
            "run_id, run_status, scorer_status, requested_provider, requested_model, "
            "effective_provider, effective_model, execution_fingerprint, duration_ms, "
            "cost_usd, cost_source, verification_attempts, first_pass_valid, "
            "recovery_attempted, recovered, verification_final_status, trace_disabled, "
            "finished_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, ?, ?)",
            rows,
        )
    def get_eval_snapshot(self, eval_run_id: str) -> dict[str, Any] | None:
        """Return indexed eval metadata, cells, and metrics without reading traces."""
        run = self._conn.execute(
            "SELECT * FROM eval_runs WHERE eval_run_id=?", (eval_run_id,)
        ).fetchone()
        if run is None:
            return None
        cells = [
            dict(row)
            for row in self._conn.execute(
                "SELECT * FROM eval_cells WHERE eval_run_id=? "
                "ORDER BY case_key, repetition, cell_id",
                (eval_run_id,),
            ).fetchall()
        ]
        metrics = []
        for row in self._conn.execute(
            "SELECT m.id, m.run_id, m.name, m.value, m.direction, m.unit, m.source, "
            "m.scope, m.metadata_json, c.case_key, c.repetition "
            "FROM run_metrics m JOIN eval_cells c ON c.run_id=m.run_id "
            "WHERE c.eval_run_id=? ORDER BY c.case_key, c.repetition, m.id",
            (eval_run_id,),
        ).fetchall():
            item = dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json"))
            metrics.append(item)
        return {"eval": dict(run), "cells": cells, "metrics": metrics}
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
        """Ingest every ``*.jsonl`` run trace in a directory. Returns run count.

        Recursive so imported or archived trace trees are ingested too.
        """
        directory = Path(trace_dir)
        if not directory.exists():
            return 0
        count = 0
        for file_path in sorted(directory.rglob("*.jsonl")):
            count += len(self.ingest_trace_file(file_path))
        return count

    def _ingest_run(self, run_id: str, events: list[dict[str, Any]], trace_path: str) -> None:
        envelope = events[0]
        harness_name = envelope.get("harness_name", "")
        # Identity: the envelope's stable harness id when the trace carries
        # one, the display name for a pre-identity trace. Every query keys on
        # this, so two harnesses sharing a *name* never share evidence.
        harness_id = envelope.get("harness_id", "") or ""
        row: dict[str, Any] = {
            "run_id": run_id,
            "harness_name": harness_name,
            "harness_id": harness_id,
            "harness_key": harness_id or harness_name,
            "harness_version_hash": envelope.get("harness_version_hash", ""),
            "status": "incomplete",
            "turns": 0,
            "cost_usd": 0.0,
            "duration_seconds": 0.0,
            "started_at": None,
            "finished_at": None,
            "reason": "",
            "trace_path": trace_path,
            "parent_run_id": None,
            "forked_at_seq": None,
            "model_path": "",
            "task": None,
            "requested_provider": "",
            "requested_model": "",
            "effective_provider": "",
            "effective_model": "",
            "execution_fingerprint": "",
        }
        verifications: list[tuple] = []
        triggers: list[tuple] = []
        visits: list[tuple] = []

        for event in events:
            etype = event.get("type")
            payload = event.get("payload", {})
            if etype == "run_started":
                row["started_at"] = event.get("timestamp")
                task = payload.get("input")
                if isinstance(task, str):
                    # Capped: a label and a search target, not a second copy of
                    # the journal, which already holds the full input.
                    row["task"] = task[:_TASK_CHARS]
                lineage = payload.get("lineage")
                if isinstance(lineage, dict):
                    row["parent_run_id"] = lineage.get("parent_run_id") or None
                    row["forked_at_seq"] = lineage.get("forked_at_seq")
            elif etype == "run_finished":
                row["status"] = payload.get("status", "incomplete")
                row["turns"] = payload.get("turns", 0)
                row["cost_usd"] = payload.get("cost_usd", 0.0)
                row["duration_seconds"] = payload.get("duration_seconds", 0.0)
                row["reason"] = payload.get("reason", "")
                row["finished_at"] = event.get("timestamp")
                row["model_path"] = payload.get("model_path", "") or ""
                execution = payload.get("execution")
                if isinstance(execution, dict):
                    row["requested_provider"] = execution.get("requested_provider", "") or ""
                    row["requested_model"] = execution.get("requested_model", "") or ""
                    row["effective_provider"] = execution.get("effective_provider", "") or ""
                    row["effective_model"] = execution.get("effective_model", "") or ""
                    row["execution_fingerprint"] = (
                        execution.get("execution_fingerprint", "") or ""
                    )
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
            elif etype == "playbook_switch":
                visits.append(
                    (
                        run_id,
                        event.get("seq", 0),
                        payload.get("to", ""),
                        payload.get("from") or "",
                        1 if payload.get("ok") else 0,
                        payload.get("refused_reason", "") or payload.get("reason", ""),
                    )
                )

        cur = self._conn
        cur.execute("DELETE FROM runs WHERE run_id=?", (run_id,))
        cur.execute("DELETE FROM verifications WHERE run_id=?", (run_id,))
        cur.execute("DELETE FROM guardrail_triggers WHERE run_id=?", (run_id,))
        cur.execute("DELETE FROM playbook_visits WHERE run_id=?", (run_id,))
        cur.execute("DELETE FROM friction_events WHERE run_id=?", (run_id,))
        cur.execute(
            "INSERT INTO runs (run_id, harness_name, harness_id, harness_key, "
            "harness_version_hash, status, turns, "
            "cost_usd, duration_seconds, started_at, finished_at, reason, trace_path, "
            "parent_run_id, forked_at_seq, model_path, task, requested_provider, "
            "requested_model, effective_provider, effective_model, execution_fingerprint) "
            "VALUES (:run_id, :harness_name, :harness_id, :harness_key, "
            ":harness_version_hash, :status, :turns, "
            ":cost_usd, :duration_seconds, :started_at, :finished_at, :reason, :trace_path, "
            ":parent_run_id, :forked_at_seq, :model_path, :task, :requested_provider, "
            ":requested_model, :effective_provider, :effective_model, "
            ":execution_fingerprint)",
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
        cur.executemany(
            "INSERT INTO playbook_visits (run_id, seq, playbook, entered_from, ok, reason) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            visits,
        )
        cur.executemany(
            "INSERT INTO friction_events (run_id, seq, category, phase, attempt, "
            "component, fingerprint, recovered, timestamp, summary) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            self._derive_friction(run_id, events, row),
        )

    @staticmethod
    def _derive_friction(
        run_id: str, events: list[dict[str, Any]], run: dict[str, Any]
    ) -> list[tuple[Any, ...]]:
        """Build bounded, redacted incident rows from one run journal.

        Trace payloads have already passed through the logging redactor. The
        Hive keeps only short diagnostics or generic descriptions, never tool
        inputs, tool output bodies, model text, or operator messages.
        """
        records: list[tuple[Any, ...]] = []
        final_success = run["status"] == "success"
        current_phase = ""
        current_turn = 0
        verification_attempt = 0
        previous_type = ""
        unmatched_model_call: dict[str, Any] | None = None

        def add(
            event: dict[str, Any],
            category: str,
            *,
            component: str = "",
            summary: str = "",
            phase: str | None = None,
            attempt: int | None = None,
            recovered: bool | None = None,
        ) -> None:
            safe_summary = normalize_feedback(str(summary or category))[:_FRICTION_SUMMARY_CHARS]
            event_phase = current_phase if phase is None else phase
            material = json.dumps(
                {
                    "category": category,
                    "phase": event_phase,
                    "component": component,
                    "summary": safe_summary,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            fingerprint = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
            records.append(
                (
                    run_id,
                    int(event.get("seq", 0)),
                    category,
                    event_phase or None,
                    current_turn if attempt is None else attempt,
                    component or None,
                    fingerprint,
                    1 if (final_success if recovered is None else recovered) else 0,
                    event.get("timestamp"),
                    safe_summary,
                )
            )

        ordered = sorted(events, key=lambda event: int(event.get("seq", 0)))
        for event in ordered:
            etype = event.get("type", "")
            payload = event.get("payload")
            if not isinstance(payload, dict):
                payload = {}

            if etype == "model_call":
                current_phase = str(payload.get("phase") or "act")
                current_turn = int(payload.get("turn") or current_turn or 0) + 1
                unmatched_model_call = event
            elif etype == "model_response":
                current_phase = str(payload.get("phase") or current_phase)
                current_turn = int(payload.get("turn") or current_turn or 0)
                unmatched_model_call = None
            elif etype == "verification_result":
                if previous_type != "verification_result":
                    verification_attempt += 1
                if not payload.get("passed"):
                    verifier = str(payload.get("verifier") or "verification")
                    lowered = verifier.lower()
                    category = (
                        "output_validation"
                        if "schema" in lowered or "output" in lowered or "json" in lowered
                        else "verifier_failure"
                    )
                    add(
                        event,
                        category,
                        component=verifier,
                        summary=str(payload.get("feedback") or f"{verifier} failed"),
                        phase="verification",
                        attempt=verification_attempt,
                    )
            elif etype == "tool_retry":
                add(
                    event,
                    "retry",
                    component=str(payload.get("name") or "tool"),
                    summary="tool call retried after a retryable error",
                )
            elif etype == "tool_result" and payload.get("is_error"):
                add(
                    event,
                    "tool_error",
                    component=str(payload.get("name") or "tool"),
                    summary="tool returned an error",
                )
            elif etype == "tool_truncated":
                add(
                    event,
                    "tool_error",
                    component=str(payload.get("name") or "tool"),
                    summary="tool call was not executed because its arguments were truncated",
                )
            elif etype == "context_overflow_recovery":
                add(
                    event,
                    "retry",
                    component="context_window",
                    summary=str(payload.get("error") or "provider context overflow recovered"),
                    phase=str(payload.get("phase") or current_phase),
                )
            elif etype == "context_compaction":
                add(
                    event,
                    "context_compaction",
                    component=str(payload.get("method") or "context"),
                    summary="context was compacted",
                    phase="compaction",
                )
            elif etype == "user_steer":
                add(
                    event,
                    "user_steer",
                    component="operator",
                    summary="operator steering was applied",
                )
            elif etype == "guardrail_triggered":
                kind = str(payload.get("kind") or "")
                if kind in {"halt", "block"}:
                    add(
                        event,
                        "guardrail_halt" if kind == "halt" else "guardrail_block",
                        component=str(payload.get("guardrail") or "guardrail"),
                        summary=str(payload.get("reason") or f"guardrail {kind}"),
                        recovered=False if kind == "halt" else None,
                    )
            elif etype == "model_swap_failed":
                add(
                    event,
                    "provider_error",
                    component=str(payload.get("requested_model") or "model_router"),
                    summary=str(payload.get("error") or "model swap failed"),
                )

            previous_type = etype

        finished = ordered[-1] if ordered else {"seq": 0, "timestamp": None}
        if run["status"] == "max_turns":
            add(
                finished,
                "loop_limit",
                component="max_turns",
                summary=str(run.get("reason") or "loop reached its model-call limit"),
                recovered=False,
            )
        if run["status"] == "error" and unmatched_model_call is not None:
            add(
                finished,
                "provider_error",
                component=str(run.get("effective_model") or run.get("requested_model") or "model"),
                summary=str(run.get("reason") or "provider request failed"),
                phase=str(unmatched_model_call.get("payload", {}).get("phase") or current_phase),
                recovered=False,
            )
        return records

    # ------------------------------------------------------------------ #
    # Queries
    # ------------------------------------------------------------------ #
    def version_stats(
        self, harness_key: str, *, include_swapped: bool = False
    ) -> list[dict[str, Any]]:
        """Per-version-hash aggregates so evolution can be judged.

        A version hash is a fitness bucket: "this harness at this version
        scored N%". A run whose model changed mid-flight did not execute the
        harness as declared, so by default it is **excluded** — averaging it in
        would silently turn the bucket into a distribution over model paths
        while still reporting a single number. ``swapped_runs`` reports how
        many were held out, so the exclusion is visible rather than tacit;
        ``include_swapped`` folds them back in for a caller that wants the
        raw population.

        A ``model_path`` naming exactly one model is not a swap — that is
        every ordinary run, including every run recorded before 1.0 (whose
        ``model_path`` is empty).
        """
        clause = "" if include_swapped else " AND (model_path IS NULL OR model_path NOT LIKE '%>%')"
        rows = self._conn.execute(
            "SELECT harness_version_hash AS version, "
            "COUNT(*) AS runs, "
            "SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS successes, "
            "AVG(cost_usd) AS avg_cost_usd, "
            "AVG(turns) AS avg_turns "
            f"FROM runs WHERE harness_key=?{clause} GROUP BY harness_version_hash "
            "ORDER BY runs DESC",
            (harness_key,),
        ).fetchall()
        swapped = {
            row["version"]: row["n"]
            for row in self._conn.execute(
                "SELECT harness_version_hash AS version, COUNT(*) AS n FROM runs "
                "WHERE harness_key=? AND model_path LIKE '%>%' "
                "GROUP BY harness_version_hash",
                (harness_key,),
            )
        }
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
                    "swapped_runs": 0 if include_swapped else swapped.get(row["version"], 0),
                }
            )
        # A version whose runs *all* swapped disappears from the grouped query
        # above. Reporting it with zero counted runs is the honest answer.
        for version, count in swapped.items():
            if include_swapped or any(r["version"] == version for r in result):
                continue
            result.append(
                {
                    "version": version,
                    "runs": 0,
                    "successes": 0,
                    "success_rate": 0.0,
                    "avg_cost_usd": 0.0,
                    "avg_turns": 0.0,
                    "swapped_runs": count,
                }
            )
        return result

    def model_path_stats(self, harness_key: str) -> list[dict[str, Any]]:
        """Per-(version, model path) aggregates: the buckets a swap creates.

        `version_stats` answers "how does this harness do"; this answers "how
        does it do on each executor it actually ran on", which is the only
        honest way to read a population that contains swapped runs.
        """
        rows = self._conn.execute(
            "SELECT harness_version_hash AS version, "
            "COALESCE(NULLIF(model_path, ''), '(pre-1.0)') AS model_path, "
            "COUNT(*) AS runs, "
            "SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS successes, "
            "AVG(cost_usd) AS avg_cost_usd "
            "FROM runs WHERE harness_key=? GROUP BY version, model_path "
            "ORDER BY runs DESC",
            (harness_key,),
        ).fetchall()
        return [
            {
                "version": row["version"],
                "model_path": row["model_path"],
                "runs": row["runs"] or 0,
                "successes": row["successes"] or 0,
                "success_rate": (row["successes"] or 0) / (row["runs"] or 1),
                "avg_cost_usd": row["avg_cost_usd"] or 0.0,
            }
            for row in rows
        ]

    def failure_signatures(
        self, harness_key: str, limit: int = 10, *, version: str | None = None
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
        # All three queries below share this clause; scoping only some of them
        # reported another version's failures.
        scope = "r.harness_key=?" + (" AND r.harness_version_hash=?" if version else "")
        args: tuple[Any, ...] = (harness_key, version) if version else (harness_key,)
        rows = self._conn.execute(
            "SELECT v.feedback AS feedback, r.run_id AS run_id "
            "FROM verifications v JOIN runs r ON v.run_id=r.run_id "
            f"WHERE {scope} AND v.passed=0 AND v.feedback != ''",  # noqa: S608
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
            f"WHERE {scope} GROUP BY g.guardrail, g.kind ORDER BY count DESC LIMIT ?",  # noqa: S608
            (*args, limit),
        ).fetchall()
        statuses = self._conn.execute(
            "SELECT r.status AS status, COUNT(*) AS count FROM runs r "
            f"WHERE {scope} AND r.status != 'success' "  # noqa: S608
            "GROUP BY r.status ORDER BY count DESC",
            args,
        ).fetchall()
        return {
            "verdicts": verdicts,
            "guardrails": [dict(r) for r in guardrails],
            "statuses": [dict(r) for r in statuses],
        }

    def recent_failures(
        self, harness_key: str, n: int = 5, *, version: str | None = None
    ) -> list[dict[str, Any]]:
        """The N most recent failed runs with their failing verifier feedback.

        ``version`` scopes them, so examples cannot come from a version a
        caller's aggregate counts excluded.
        """
        scope = "harness_key=?" + (" AND harness_version_hash=?" if version else "")
        args: tuple[Any, ...] = (harness_key, version, n) if version else (harness_key, n)
        runs = self._conn.execute(
            f"SELECT * FROM runs WHERE {scope} AND status != 'success' "  # noqa: S608
            "ORDER BY finished_at DESC LIMIT ?",
            args,
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
        """Fetch a single run with its verification, guardrail, and friction records."""
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
        entry["friction"] = [
            _friction_row(row)
            for row in self._conn.execute(
                "SELECT seq, category, phase, attempt, component, fingerprint, recovered, "
                "timestamp, summary FROM friction_events WHERE run_id=? ORDER BY seq, id",
                (run_id,),
            ).fetchall()
        ]
        return entry

    def list_friction(
        self,
        harness_key: str,
        *,
        category: str | None = None,
        component: str | None = None,
        recovered: bool | None = None,
        model: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List friction records with stable filters over run provenance."""
        if limit < 1:
            raise ValueError("limit must be at least 1")
        where, params = _friction_filters(
            harness_key,
            category=category,
            component=component,
            recovered=recovered,
            model=model,
            since=since,
            until=until,
        )
        rows = self._conn.execute(
            "SELECT f.run_id, f.seq, f.category, f.phase, f.attempt, f.component, "
            "f.fingerprint, f.recovered, f.timestamp, f.summary, r.status AS run_status, "
            "COALESCE(NULLIF(r.effective_model, ''), NULLIF(r.requested_model, ''), "
            "NULLIF(r.model_path, ''), '') AS model "
            "FROM friction_events f JOIN runs r ON r.run_id=f.run_id "
            f"WHERE {' AND '.join(where)} ORDER BY f.timestamp DESC, f.seq DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [_friction_row(row) for row in rows]

    def friction_summary(
        self,
        harness_key: str,
        *,
        category: str | None = None,
        component: str | None = None,
        recovered: bool | None = None,
        model: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> dict[str, Any]:
        """Aggregate friction without reading raw journals."""
        where, params = _friction_filters(
            harness_key,
            category=category,
            component=component,
            recovered=recovered,
            model=model,
            since=since,
            until=until,
        )
        scope = " AND ".join(where)
        totals = self._conn.execute(
            "SELECT COUNT(*) AS events, COUNT(DISTINCT f.run_id) AS runs, "
            "SUM(CASE WHEN f.recovered=1 THEN 1 ELSE 0 END) AS recovered "
            "FROM friction_events f JOIN runs r ON r.run_id=f.run_id "
            f"WHERE {scope}",
            params,
        ).fetchone()
        categories = self._conn.execute(
            "SELECT f.category, COUNT(*) AS events, COUNT(DISTINCT f.run_id) AS runs, "
            "SUM(CASE WHEN f.recovered=1 THEN 1 ELSE 0 END) AS recovered "
            "FROM friction_events f JOIN runs r ON r.run_id=f.run_id "
            f"WHERE {scope} GROUP BY f.category ORDER BY events DESC, f.category",
            params,
        ).fetchall()

        def aggregate(row: sqlite3.Row) -> dict[str, Any]:
            events = row["events"] or 0
            recovered_events = row["recovered"] or 0
            return {
                "category": row["category"],
                "events": events,
                "runs": row["runs"] or 0,
                "recovered": recovered_events,
                "unrecovered": events - recovered_events,
            }

        total_events = totals["events"] or 0
        recovered_events = totals["recovered"] or 0
        return {
            "events": total_events,
            "runs": totals["runs"] or 0,
            "recovered": recovered_events,
            "unrecovered": total_events - recovered_events,
            "categories": [aggregate(row) for row in categories],
        }

    def lineage(self, run_id: str) -> dict[str, Any]:
        """The fork tree around a run: its ancestors, itself, and its forks.

        A fork re-enters its parent at a journal seq, so the two runs share
        everything up to that point. Reporting them as unrelated whole runs
        would throw away the only thing that makes the comparison sharp — that
        the prefix is identical and exactly one thing downstream changed.
        """
        run = self.get_run(run_id)
        if run is None:
            return {"run": None, "ancestors": [], "forks": []}

        ancestors: list[dict[str, Any]] = []
        seen = {run_id}
        cursor = run.get("parent_run_id")
        while cursor and cursor not in seen:
            seen.add(cursor)
            parent = self.get_run(cursor)
            if parent is None:
                ancestors.append({"run_id": cursor, "missing": True})
                break
            ancestors.append(parent)
            cursor = parent.get("parent_run_id")

        forks = [
            dict(row)
            for row in self._conn.execute(
                "SELECT * FROM runs WHERE parent_run_id=? ORDER BY started_at", (run_id,)
            )
        ]
        return {"run": run, "ancestors": ancestors, "forks": forks}

    def search_runs(
        self, query: str, *, harness_key: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Runs whose task statement contains ``query`` (case-insensitive).

        A LIKE scan over the indexed task text, not a full-text index over
        every message. That is the honest scope: the Hive indexes runs, and
        this makes the thing a person searches for — what they asked — findable
        without pretending to search the transcript.
        """
        term = query.strip()
        if not term:
            return []
        where = ["task IS NOT NULL", "task LIKE ? ESCAPE '\\'"]
        escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params: list[Any] = [f"%{escaped}%"]
        if harness_key is not None:
            where.append("harness_key=?")
            params.append(harness_key)
        return [
            dict(row)
            for row in self._conn.execute(
                f"SELECT * FROM runs WHERE {' AND '.join(where)} "
                "ORDER BY started_at DESC LIMIT ?",
                (*params, limit),
            )
        ]

    # ------------------------------------------------------------------ #
    # Comparison
    # ------------------------------------------------------------------ #
    def compare_versions(self, harness_key: str, left: str, right: str) -> dict[str, Any]:
        """Two harness versions side by side, with the deltas spelled out.

        This is what makes an evolution or a fork *judgeable*: not "the new one
        scored 71%" but "+18 points, -$0.004 per run, and this failure
        signature stopped appearing". Deltas are right-minus-left, so a
        positive ``success_rate`` means the right-hand version is better.
        """
        by_version = {row["version"]: row for row in self.version_stats(harness_key)}

        def side(version: str) -> dict[str, Any]:
            stats = by_version.get(version) or {
                "version": version,
                "runs": 0,
                "successes": 0,
                "success_rate": 0.0,
                "avg_cost_usd": 0.0,
                "avg_turns": 0.0,
                "swapped_runs": 0,
            }
            return {
                **stats,
                "failures": self.failure_signatures(harness_key, version=version),
            }

        a, b = side(left), side(right)
        # A signature present on one side and absent on the other is the most
        # actionable single fact in the comparison, so name both directions.
        def _keys(entry: dict[str, Any]) -> set[str]:
            # `failure_signatures` groups verdicts by normalised feedback, so
            # that string *is* the signature identity.
            return {
                str(item["feedback"])
                for item in entry["failures"].get("verdicts", [])
                if item.get("feedback")
            }

        left_keys, right_keys = _keys(a), _keys(b)
        return {
            "harness_key": harness_key,
            "left": a,
            "right": b,
            "delta": {
                "success_rate": b["success_rate"] - a["success_rate"],
                "avg_cost_usd": b["avg_cost_usd"] - a["avg_cost_usd"],
                "avg_turns": b["avg_turns"] - a["avg_turns"],
                "runs": b["runs"] - a["runs"],
            },
            "fixed_failures": sorted(left_keys - right_keys),
            "new_failures": sorted(right_keys - left_keys),
            # Neither side is comparable on one run each; say so rather than
            # letting a UI render a confident delta over a sample of two.
            "underpowered": a["runs"] < 5 or b["runs"] < 5,
        }

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
        self._conn.execute(
            f"DELETE FROM playbook_visits WHERE run_id IN ({placeholders})", run_ids
        )
        self._conn.execute(
            f"DELETE FROM run_outcomes WHERE run_id IN ({placeholders})", run_ids
        )
        self._conn.execute(
            f"DELETE FROM friction_events WHERE run_id IN ({placeholders})", run_ids
        )
        self._conn.execute(
            f"DELETE FROM run_metrics WHERE run_id IN ({placeholders})", run_ids
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

    def failure_count(
        self, harness_key: str, *, since: str | None = None, version: str | None = None
    ) -> int:
        """COUNT(*) of non-success runs, optionally since an ISO timestamp.

        Gate on this and then analyse? Pass the same ``version`` to both, or the
        gate opens on evidence the analysis will not see.
        """
        query = "SELECT COUNT(*) AS n FROM runs WHERE harness_key=? AND status != 'success'"
        params: list[Any] = [harness_key]
        if version is not None:
            query += " AND harness_version_hash=?"
            params.append(version)
        if since is not None:
            query += " AND finished_at >= ?"
            params.append(since)
        row = self._conn.execute(query, params).fetchone()
        return row["n"] or 0

    # ------------------------------------------------------------------ #
    # Numeric run metrics
    # ------------------------------------------------------------------ #
    @staticmethod
    def _same_metric(left: dict[str, Any], right: dict[str, Any]) -> bool:
        fields = (
            "run_id",
            "name",
            "value",
            "direction",
            "unit",
            "source",
            "scope",
            "metadata_json",
        )
        return all(left[field] == right[field] for field in fields)

    def record_metrics(
        self, harness_key: str, rows: list[dict[str, Any]]
    ) -> dict[str, int]:
        """Transactionally insert validated metric rows.

        Exact idempotency collisions are skipped. Reusing a key for different
        content fails the entire batch before any row is inserted.
        """
        unique: dict[str, dict[str, Any]] = {}
        duplicates = 0
        for row in rows:
            key = row["idempotency_key"]
            prior = unique.get(key)
            if prior is None:
                unique[key] = row
            elif self._same_metric(prior, row):
                duplicates += 1
            else:
                raise ValueError(
                    f"idempotency key {key!r} is reused for different metric content"
                )

        try:
            self._conn.execute("BEGIN IMMEDIATE")
            for row in unique.values():
                run = self._conn.execute(
                    "SELECT harness_key FROM runs WHERE run_id=?", (row["run_id"],)
                ).fetchone()
                if run is None:
                    raise ValueError(f"metric run_id {row['run_id']!r} is not indexed")
                if run["harness_key"] != harness_key:
                    raise ValueError(
                        f"metric run_id {row['run_id']!r} belongs to a different harness"
                    )
                existing = self._conn.execute(
                    "SELECT * FROM run_metrics WHERE idempotency_key=?",
                    (row["idempotency_key"],),
                ).fetchone()
                if existing is None:
                    continue
                if not self._same_metric(dict(existing), row):
                    raise ValueError(
                        f"idempotency key {row['idempotency_key']!r} already records "
                        "different metric content"
                    )
                duplicates += 1

            inserted = 0
            for row in unique.values():
                cursor = self._conn.execute(
                    "INSERT OR IGNORE INTO run_metrics "
                    "(idempotency_key, run_id, name, value, direction, unit, source, "
                    "scope, metadata_json, recorded_at) VALUES "
                    "(:idempotency_key, :run_id, :name, :value, :direction, :unit, "
                    ":source, :scope, :metadata_json, :recorded_at)",
                    row,
                )
                inserted += cursor.rowcount
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return {"received": len(rows), "inserted": inserted, "duplicates": duplicates}

    @staticmethod
    def _metric_filters(
        harness_key: str,
        *,
        run_id: str | None,
        name: str | None,
        source: str | None,
        scope: str | None,
        model: str | None,
        since: str | None,
        until: str | None,
    ) -> tuple[list[str], list[Any]]:
        where = ["r.harness_key=?"]
        params: list[Any] = [harness_key]
        for column, value in (
            ("m.run_id", run_id),
            ("m.name", name),
            ("m.source", source),
            ("m.scope", scope),
        ):
            if value is not None:
                where.append(f"{column}=?")
                params.append(value)
        if model is not None:
            where.append(
                "COALESCE(NULLIF(r.effective_model, ''), "
                "NULLIF(r.requested_model, ''), NULLIF(r.model_path, ''), '')=?"
            )
            params.append(model)
        if since is not None:
            where.append("r.finished_at>=?")
            params.append(since)
        if until is not None:
            where.append("r.finished_at<=?")
            params.append(until)
        return where, params

    def list_metrics(
        self,
        harness_key: str,
        *,
        run_id: str | None = None,
        name: str | None = None,
        source: str | None = None,
        scope: str | None = None,
        model: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int | None = 1000,
    ) -> list[dict[str, Any]]:
        """List immutable metrics with their run provenance."""
        if limit is not None and limit < 1:
            raise ValueError("metric limit must be at least 1")
        where, params = self._metric_filters(
            harness_key,
            run_id=run_id,
            name=name,
            source=source,
            scope=scope,
            model=model,
            since=since,
            until=until,
        )
        query = (
            "SELECT m.id, m.idempotency_key, m.run_id, m.name, m.value, m.direction, "
            "m.unit, m.source, m.scope, m.metadata_json, m.recorded_at, "
            "r.finished_at AS run_finished_at, r.harness_version_hash AS behavior_hash, "
            "COALESCE(NULLIF(r.effective_model, ''), NULLIF(r.requested_model, ''), "
            "NULLIF(r.model_path, ''), '') AS model "
            "FROM run_metrics m JOIN runs r ON r.run_id=m.run_id "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY r.finished_at DESC, m.id DESC"
        )
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        rows = self._conn.execute(query, params).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json"))
            result.append(item)
        return result

    def metric_aggregates(
        self,
        harness_key: str,
        *,
        run_id: str | None = None,
        name: str | None = None,
        source: str | None = None,
        scope: str | None = None,
        model: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> list[dict[str, Any]]:
        """Aggregate without mixing names, sources, scopes, units, or directions."""
        metrics = self.list_metrics(
            harness_key,
            run_id=run_id,
            name=name,
            source=source,
            scope=scope,
            model=model,
            since=since,
            until=until,
            limit=None,
        )
        run_where = ["harness_key=?"]
        run_params: list[Any] = [harness_key]
        if run_id is not None:
            run_where.append("run_id=?")
            run_params.append(run_id)
        if model is not None:
            run_where.append(
                "COALESCE(NULLIF(effective_model, ''), NULLIF(requested_model, ''), "
                "NULLIF(model_path, ''), '')=?"
            )
            run_params.append(model)
        if since is not None:
            run_where.append("finished_at>=?")
            run_params.append(since)
        if until is not None:
            run_where.append("finished_at<=?")
            run_params.append(until)
        eligible = self._conn.execute(
            f"SELECT COUNT(*) AS n FROM runs WHERE {' AND '.join(run_where)}",
            run_params,
        ).fetchone()["n"]

        groups: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = {}
        for metric in metrics:
            key = (
                metric["name"],
                metric["source"],
                metric["scope"],
                metric["unit"],
                metric["direction"],
            )
            groups.setdefault(key, []).append(metric)

        output: list[dict[str, Any]] = []
        for key, records in sorted(groups.items()):
            metric_name, metric_source, metric_scope, metric_unit, direction = key
            values = [record["value"] for record in records]
            observed_runs = len({record["run_id"] for record in records})
            population = len(records) if metric_scope == "eval" else eligible
            missing = 0 if metric_scope == "eval" else max(0, eligible - observed_runs)
            output.append(
                {
                    "name": metric_name,
                    "source": metric_source,
                    "scope": metric_scope,
                    "unit": metric_unit,
                    "direction": direction,
                    "sample_count": len(values),
                    "observed_run_count": observed_runs,
                    "population_count": population,
                    "missing_value_count": missing,
                    "mean": sum(values) / len(values),
                    "min": min(values),
                    "max": max(values),
                }
            )
        return output

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

    # ------------------------------------------------------------------ #
    # Deferred outcomes
    # ------------------------------------------------------------------ #
    def record_outcome(
        self,
        run_id: str,
        outcome: str,
        *,
        source: str = "external",
        detail: str = "",
    ) -> dict[str, Any]:
        """Attach a real-world outcome to a completed run, after the fact.

        Validators grade a run *while it happens*, from what the output looks
        like. Some signals only exist later and elsewhere: a human confirmed
        or dismissed the proposal, the campaign converted, the extracted
        record turned out wrong. This records that judgement against the
        run_id so it can drive evolution.

        ``outcome`` is ``"success"`` or ``"failure"``; anything else is
        rejected, because :meth:`outcome_summary` and the analyzer both treat
        this as a binary reward signal. The run row itself is never rewritten
        — the trace remains what the run *did*, and this stays what the world
        later said about it. One outcome per run: recording again replaces it,
        so a corrected label wins.
        """
        if outcome not in ("success", "failure"):
            raise ValueError(
                f"outcome must be 'success' or 'failure' (got {outcome!r})"
            )
        row = self._conn.execute(
            "SELECT run_id FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown run_id '{run_id}'")
        recorded_at = datetime.now(UTC).isoformat()
        self._conn.execute(
            "INSERT INTO run_outcomes (run_id, outcome, source, detail, recorded_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(run_id) DO UPDATE SET "
            "outcome=excluded.outcome, source=excluded.source, "
            "detail=excluded.detail, recorded_at=excluded.recorded_at",
            (run_id, outcome, source, detail, recorded_at),
        )
        self._conn.commit()
        return {
            "run_id": run_id,
            "outcome": outcome,
            "source": source,
            "detail": detail,
            "recorded_at": recorded_at,
        }

    def get_outcome(self, run_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT run_id, outcome, source, detail, recorded_at "
            "FROM run_outcomes WHERE run_id=?",
            (run_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def outcome_summary(
        self, harness_key: str, *, version: str | None = None
    ) -> dict[str, Any]:
        """Labelled-outcome stats for one harness.

        Separate from :meth:`summary`'s ``success_rate`` on purpose: that one
        measures whether the harness satisfied its own validators, this one
        whether the result held up in the world. They can disagree, and the
        disagreement is the interesting part.
        """
        query = (
            "SELECT o.outcome AS outcome, COUNT(*) AS n "
            "FROM run_outcomes o JOIN runs r ON r.run_id = o.run_id "
            "WHERE r.harness_key=?"
        )
        params: list[Any] = [harness_key]
        if version is not None:
            query += " AND r.harness_version_hash=?"
            params.append(version)
        query += " GROUP BY o.outcome"
        counts = {r["outcome"]: r["n"] for r in self._conn.execute(query, params)}
        labelled = sum(counts.values())
        successes = counts.get("success", 0)
        return {
            "harness_key": harness_key,
            "labelled_runs": labelled,
            "successes": successes,
            "failures": counts.get("failure", 0),
            "outcome_success_rate": (successes / labelled) if labelled else 0.0,
        }

    def failed_outcome_traces(
        self, harness_key: str, limit: int = 5, *, version: str | None = None
    ) -> list[dict[str, Any]]:
        """Recent runs the world labelled as failures, newest first."""
        query = (
            "SELECT r.run_id, r.trace_path, r.status, o.detail, o.source, o.recorded_at "
            "FROM run_outcomes o JOIN runs r ON r.run_id = o.run_id "
            "WHERE r.harness_key=? AND o.outcome='failure'"
        )
        params: list[Any] = [harness_key]
        if version is not None:
            query += " AND r.harness_version_hash=?"
            params.append(version)
        query += " ORDER BY o.recorded_at DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self._conn.execute(query, params).fetchall()]

    def playbook_stats(
        self, harness_key: str, *, version: str | None = None
    ) -> list[dict[str, Any]]:
        """Success, cost, and turns per playbook.

        Attribution is *by visit*: a run that worked in two playbooks counts
        once for each, so the rates are "runs that passed through this mode",
        not an exclusive partition. That is the honest reading of a single
        run-level outcome, and it is still enough to separate a mode that
        keeps failing from one that does not — which is what lets evolution
        target one mode's prompt.

        ``refusals`` counts switches the gates rejected; a mode nobody can
        enter looks healthy on success rate alone.
        """
        query = (
            "SELECT v.playbook AS playbook, "
            "COUNT(DISTINCT CASE WHEN v.ok=1 THEN v.run_id END) AS runs, "
            "COUNT(DISTINCT CASE WHEN v.ok=1 AND r.status='success' "
            "                    THEN v.run_id END) AS successes, "
            "SUM(CASE WHEN v.ok=0 THEN 1 ELSE 0 END) AS refusals, "
            "AVG(r.cost_usd) AS avg_cost_usd, AVG(r.turns) AS avg_turns "
            "FROM playbook_visits v JOIN runs r ON r.run_id = v.run_id "
            "WHERE r.harness_key=?"
        )
        params: list[Any] = [harness_key]
        if version is not None:
            query += " AND r.harness_version_hash=?"
            params.append(version)
        query += " GROUP BY v.playbook ORDER BY runs DESC, playbook"

        out: list[dict[str, Any]] = []
        for row in self._conn.execute(query, params).fetchall():
            runs = row["runs"] or 0
            out.append(
                {
                    "playbook": row["playbook"],
                    "runs": runs,
                    "success_rate": ((row["successes"] or 0) / runs) if runs else 0.0,
                    "refusals": row["refusals"] or 0,
                    "avg_cost_usd": row["avg_cost_usd"] or 0.0,
                    "avg_turns": row["avg_turns"] or 0.0,
                }
            )
        return out

    def summary(self, harness_key: str, *, display_name: str | None = None) -> dict[str, Any]:
        """A rolled-up stats view for one harness (all versions + per version).

        ``display_name`` labels the result when the caller knows the spec's
        human name; without it the name is resolved from the newest run.
        """
        totals = self._conn.execute(
            "SELECT COUNT(*) AS runs, "
            "SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS successes, "
            "AVG(cost_usd) AS avg_cost_usd, AVG(turns) AS avg_turns "
            "FROM runs WHERE harness_key=?",
            (harness_key,),
        ).fetchone()
        runs = totals["runs"] or 0
        successes = totals["successes"] or 0
        # The key is an opaque id for a post-1.0 harness; resolve the display
        # name from its newest run so headers stay human-readable.
        name_row = self._conn.execute(
            "SELECT harness_name FROM runs WHERE harness_key=? AND harness_name != '' "
            "ORDER BY started_at DESC LIMIT 1",
            (harness_key,),
        ).fetchone()
        resolved = name_row["harness_name"] if name_row else harness_key
        return {
            "harness_key": harness_key,
            "harness_name": display_name or resolved,
            "total_runs": runs,
            "success_rate": (successes / runs) if runs else 0.0,
            "avg_cost_usd": totals["avg_cost_usd"] or 0.0,
            "avg_turns": totals["avg_turns"] or 0.0,
            "versions": self.version_stats(harness_key),
            "failure_signatures": self.failure_signatures(harness_key),
            "playbooks": self.playbook_stats(harness_key),
            # Only worth showing when a run actually changed models; for every
            # other harness this is a single row that repeats `versions`.
            "model_paths": [
                row for row in self.model_path_stats(harness_key) if ">" in row["model_path"]
            ],
        }
