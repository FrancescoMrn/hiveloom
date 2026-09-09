"""Numeric run metrics remain transactional, queryable, and scope-safe."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from hiveloom.cli import app
from hiveloom.errors import ExitCode
from hiveloom.logging.hive import Hive
from hiveloom.logging.trace import TraceWriter
from hiveloom.metrics import RunMetric, record_run_metrics

cli = CliRunner()
HARNESS_KEY = "hl-metrics-fixture"


def _run_trace(tmp_path: Path, run_id: str, *, model: str = "model-a") -> Path:
    writer = TraceWriter(
        tmp_path / "traces",
        run_id,
        "metrics-fixture",
        "behavior-1",
        harness_id=HARNESS_KEY,
    )
    writer.emit("run_started", input=f"synthetic {run_id}")
    writer.emit(
        "run_finished",
        status="success",
        turns=1,
        cost_usd=0.01,
        duration_seconds=0.1,
        execution={
            "requested_provider": "fixture",
            "requested_model": model,
            "effective_provider": "fixture",
            "effective_model": model,
            "execution_fingerprint": f"fingerprint-{run_id}",
        },
    )
    return writer.path


def _ingest_runs(hive: Hive, tmp_path: Path, count: int = 3) -> None:
    for index in range(count):
        model = "model-b" if index == count - 1 else "model-a"
        hive.ingest_trace_file(_run_trace(tmp_path, f"run_{index}", model=model))


def _metric(
    run_id: str,
    *,
    name: str = "recall_at_5",
    value: float = 0.4,
    scope: str = "run",
    source: str = "matching_eval_v1",
    idempotency_key: str | None = None,
) -> RunMetric:
    return RunMetric(
        run_id=run_id,
        name=name,
        value=value,
        direction="maximize",
        unit="ratio",
        source=source,
        scope=scope,
        metadata={"case": run_id},
        idempotency_key=idempotency_key,
    )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_metric_values_must_be_finite(value: float):
    with pytest.raises(ValidationError, match="finite"):
        _metric("run_0", value=value)


def test_metric_metadata_is_json_safe_and_bounded():
    with pytest.raises(ValidationError, match="JSON-safe"):
        RunMetric(
            run_id="run_0",
            name="quality",
            value=1,
            direction="maximize",
            unit="ratio",
            source="fixture",
            metadata={"bad": object()},
        )
    with pytest.raises(ValidationError, match="byte limit"):
        RunMetric(
            run_id="run_0",
            name="quality",
            value=1,
            direction="maximize",
            unit="ratio",
            source="fixture",
            metadata={"large": "x" * 20_000},
        )


def test_record_is_idempotent_and_collision_is_rejected(tmp_path: Path):
    with Hive(tmp_path / "hive.db") as hive:
        _ingest_runs(hive, tmp_path, count=1)
        metric = _metric("run_0")
        first = record_run_metrics(hive, HARNESS_KEY, [metric])
        duplicate = record_run_metrics(hive, HARNESS_KEY, [metric])
        with pytest.raises(ValueError, match="different metric content"):
            record_run_metrics(
                hive,
                HARNESS_KEY,
                [_metric("run_0", value=0.8, idempotency_key=metric.resolved_idempotency_key())],
            )
        stored = hive.list_metrics(HARNESS_KEY)

    assert first == {"received": 1, "inserted": 1, "duplicates": 0}
    assert duplicate == {"received": 1, "inserted": 0, "duplicates": 1}
    assert len(stored) == 1
    assert stored[0]["metadata"] == {"case": "run_0"}


def test_batch_with_missing_run_rolls_back_every_metric(tmp_path: Path):
    with Hive(tmp_path / "hive.db") as hive:
        _ingest_runs(hive, tmp_path, count=1)
        with pytest.raises(ValueError, match="not indexed"):
            record_run_metrics(
                hive,
                HARNESS_KEY,
                [_metric("run_0"), _metric("missing")],
            )
        assert hive.list_metrics(HARNESS_KEY) == []


def test_queries_filter_provenance_and_aggregates_report_missing(tmp_path: Path):
    with Hive(tmp_path / "hive.db") as hive:
        _ingest_runs(hive, tmp_path, count=3)
        record_run_metrics(
            hive,
            HARNESS_KEY,
            [_metric("run_0", value=0.2), _metric("run_1", value=0.6)],
        )
        filtered = hive.list_metrics(
            HARNESS_KEY,
            name="recall_at_5",
            source="matching_eval_v1",
            model="model-a",
            since="2000-01-01T00:00:00+00:00",
            until="2100-01-01T00:00:00+00:00",
        )
        [aggregate] = hive.metric_aggregates(
            HARNESS_KEY,
            name="recall_at_5",
            source="matching_eval_v1",
            scope="run",
        )

    assert {row["run_id"] for row in filtered} == {"run_0", "run_1"}
    assert aggregate["sample_count"] == 2
    assert aggregate["observed_run_count"] == 2
    assert aggregate["population_count"] == 3
    assert aggregate["missing_value_count"] == 1
    assert aggregate["mean"] == pytest.approx(0.4)


def test_case_run_and_eval_scopes_never_mix(tmp_path: Path):
    with Hive(tmp_path / "hive.db") as hive:
        _ingest_runs(hive, tmp_path, count=2)
        record_run_metrics(
            hive,
            HARNESS_KEY,
            [
                _metric("run_0", name="quality", scope="case"),
                _metric("run_0", name="quality", scope="run"),
                _metric("run_0", name="quality", scope="eval"),
            ],
        )
        aggregates = hive.metric_aggregates(HARNESS_KEY, name="quality")

    assert {row["scope"] for row in aggregates} == {"case", "run", "eval"}
    eval_row = next(row for row in aggregates if row["scope"] == "eval")
    assert eval_row["sample_count"] == 1
    assert eval_row["missing_value_count"] == 0


def test_cli_record_list_and_transactional_import(monkeypatch, tmp_path: Path):
    db = tmp_path / "hive.db"
    monkeypatch.setenv("HIVELOOM_DB", str(db))
    with Hive(db) as hive:
        _ingest_runs(hive, tmp_path, count=2)

    recorded = cli.invoke(
        app,
        [
            "metrics",
            "record",
            HARNESS_KEY,
            "--run-id",
            "run_0",
            "--name",
            "recall_at_5",
            "--value",
            "0.4",
            "--direction",
            "maximize",
            "--unit",
            "ratio",
            "--source",
            "matching_eval_v1",
            "--json",
        ],
    )
    listed = cli.invoke(
        app,
        [
            "metrics",
            "list",
            HARNESS_KEY,
            "--name",
            "recall_at_5",
            "--json",
        ],
    )
    import_file = tmp_path / "metrics.ndjson"
    import_file.write_text(
        json.dumps(_metric("run_1", name="ndcg").model_dump(mode="json"))
        + "\n"
        + '{"run_id":"run_1","name":"broken","value":NaN,'
        '"direction":"maximize","unit":"ratio","source":"fixture"}\n',
        encoding="utf-8",
    )
    imported = cli.invoke(
        app,
        ["metrics", "import", HARNESS_KEY, str(import_file), "--json"],
    )

    assert recorded.exit_code == ExitCode.OK
    assert json.loads(recorded.stdout)["inserted"] == 1
    assert listed.exit_code == ExitCode.OK
    listed_payload = json.loads(listed.stdout)
    assert listed_payload["metrics"][0]["name"] == "recall_at_5"
    assert listed_payload["aggregates"][0]["missing_value_count"] == 1
    assert imported.exit_code == ExitCode.SPEC_ERROR
    assert "finite" in json.loads(imported.stdout)["error"]
    with Hive(db) as hive:
        assert hive.list_metrics(HARNESS_KEY, name="ndcg") == []


def test_cli_schema_and_idempotent_valid_import(monkeypatch, tmp_path: Path):
    db = tmp_path / "hive.db"
    monkeypatch.setenv("HIVELOOM_DB", str(db))
    with Hive(db) as hive:
        _ingest_runs(hive, tmp_path, count=1)
    import_file = tmp_path / "metrics.ndjson"
    import_file.write_text(
        json.dumps(_metric("run_0").model_dump(mode="json")) + "\n",
        encoding="utf-8",
    )

    schema = cli.invoke(app, ["metrics", "schema", "--json"])
    first = cli.invoke(
        app,
        ["metrics", "import", HARNESS_KEY, str(import_file), "--json"],
    )
    repeated = cli.invoke(
        app,
        ["metrics", "import", HARNESS_KEY, str(import_file), "--json"],
    )

    assert schema.exit_code == ExitCode.OK
    schema_payload = json.loads(schema.stdout)
    assert schema_payload["schema"]["title"] == "RunMetric"
    assert first.exit_code == ExitCode.OK
    assert json.loads(first.stdout)["inserted"] == 1
    assert repeated.exit_code == ExitCode.OK
    assert json.loads(repeated.stdout)["duplicates"] == 1


def test_old_hive_database_gains_metrics_table(tmp_path: Path):
    db = tmp_path / "legacy.db"
    connection = sqlite3.connect(db)
    connection.execute(
        "CREATE TABLE runs (run_id TEXT PRIMARY KEY, harness_name TEXT, "
        "harness_version_hash TEXT, status TEXT, turns INTEGER, cost_usd REAL, "
        "duration_seconds REAL, started_at TEXT, finished_at TEXT, reason TEXT, "
        "trace_path TEXT)"
    )
    connection.commit()
    connection.close()

    with Hive(db) as hive:
        tables = {
            row["name"]
            for row in hive._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }

    assert "run_metrics" in tables


def test_pruning_run_removes_its_metrics(tmp_path: Path):
    with Hive(tmp_path / "hive.db") as hive:
        _ingest_runs(hive, tmp_path, count=1)
        record_run_metrics(hive, HARNESS_KEY, [_metric("run_0")])
        hive._conn.execute(
            "UPDATE runs SET finished_at='2020-01-01T00:00:00+00:00' WHERE run_id='run_0'"
        )
        hive._conn.commit()
        removed = hive.prune_runs(30, now=datetime(2026, 1, 1, tzinfo=UTC))
        remaining = hive._conn.execute("SELECT COUNT(*) FROM run_metrics").fetchone()[0]

    assert removed == 1
    assert remaining == 0
