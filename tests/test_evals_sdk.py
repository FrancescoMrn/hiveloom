"""Versioned eval specs and post-verification scorer SDK."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from hiveloom import catalog, construct, ext
from hiveloom.cli import app
from hiveloom.errors import ExitCode, SpecError
from hiveloom.evals import (
    DatasetSpec,
    EvalCase,
    EvalSpec,
    ScorerOutput,
    case_model_input,
    eval_identity,
    load_eval_cases,
    load_eval_spec,
    run_scorers,
)
from hiveloom.logging.hive import Hive
from hiveloom.logging.trace import TraceWriter
from hiveloom.loop.agent_loop import RunResult
from hiveloom.metrics import RunMetric

cli = CliRunner()
HARNESS_KEY = "hl-eval-fixture"


def _register_eval_components(*, bad_scorer: bool = False) -> None:
    api = ext.ExtensionAPI(source="test:eval")
    api.register_dataset(
        "fake_cases",
        lambda params, _ctx: lambda: [
            EvalCase(
                id="case-1",
                input="public task",
                expected={"answer": params.get("answer", "yes")},
            )
        ],
        description="One synthetic case.",
        params=[{"name": "answer", "type": "str"}],
    )

    def build_scorer(params, _ctx):
        if bad_scorer:
            def fail(_context):
                raise RuntimeError("synthetic scorer failure")

            return fail

        def score(context):
            matched = context.expected["answer"] in context.run_result.output
            return ScorerOutput(
                metrics=[
                    RunMetric(
                        run_id=context.run_result.run_id,
                        name="exact_match",
                        value=float(matched),
                        direction="maximize",
                        unit="ratio",
                        source="fake_scorer",
                        scope="case",
                    )
                ],
                diagnostics=[
                    {"code": "compared", "message": "synthetic comparison complete"}
                ],
            )

        return score

    api.register_scorer(
        "fake_exact",
        build_scorer,
        description="Synthetic exact-match scorer.",
        params=[{"name": "threshold", "type": "float", "default": 1.0}],
    )


def _eval_spec(*, expected_in_input: bool = False, threshold: float = 1.0) -> EvalSpec:
    return EvalSpec(
        harness="harness",
        dataset={
            "loader": "fake_cases",
            "params": {"answer": "yes"},
            "include_expected_in_input": expected_in_input,
        },
        scorers=[{"name": "fake_exact", "params": {"threshold": threshold}}],
        repetitions=2,
    )


def _indexed_run(hive: Hive, tmp_path: Path, result: RunResult) -> None:
    writer = TraceWriter(
        tmp_path / "traces",
        result.run_id,
        "eval-fixture",
        "behavior-1",
        harness_id=HARNESS_KEY,
    )
    writer.emit("run_started", input="synthetic")
    writer.emit(
        "run_finished",
        status=result.status,
        output=result.output,
        turns=1,
        cost_usd=0.0,
        duration_seconds=0.1,
    )
    hive.ingest_trace_file(writer.path)


def test_eval_catalog_and_schema_accept_short_scorer_refs():
    _register_eval_components()
    spec = EvalSpec(
        harness="./h",
        dataset={"loader": "fake_cases"},
        scorers=["fake_exact"],
    )

    assert spec.schema_version == 1
    assert spec.scorers[0].name == "fake_exact"
    assert catalog.CATALOGS["datasets"]["fake_cases"].source == "test:eval"
    assert catalog.CATALOGS["scorers"]["fake_exact"].source == "test:eval"


def test_eval_alias_identity_requires_an_explicit_alias_set():
    _register_eval_components()

    with pytest.raises(ValueError, match="requires at least one model alias"):
        EvalSpec(
            harness="./h",
            dataset={"loader": "fake_cases"},
            scorers=["fake_exact"],
            model_identity="alias",
        )


def test_fake_dataset_and_scorer_ingest_without_provider_credentials(tmp_path: Path):
    _register_eval_components()
    spec = _eval_spec()
    [case] = load_eval_cases(spec, tmp_path)
    result = RunResult(status="success", output="yes", run_id="run_eval_1")

    with Hive(tmp_path / "hive.db") as hive:
        _indexed_run(hive, tmp_path, result)
        scoring = run_scorers(
            spec,
            case,
            result,
            base_dir=tmp_path,
            hive=hive,
            harness_key=HARNESS_KEY,
        )
        stored = hive.list_metrics(HARNESS_KEY)

    assert scoring.status == "success"
    assert scoring.run_status == "success"
    assert scoring.ingestion == {"received": 1, "inserted": 1, "duplicates": 0}
    assert stored[0]["name"] == "exact_match"


def test_expected_data_is_excluded_from_model_input_unless_requested():
    _register_eval_components()
    case = EvalCase(id="private", input="rank candidates", expected={"ids": ["secret-id"]})

    default_input = case_model_input(
        case,
        DatasetSpec(loader="fake_cases"),
    )
    opted_in = case_model_input(
        case,
        DatasetSpec(loader="fake_cases", include_expected_in_input=True),
    )

    assert default_input == "rank candidates"
    assert "secret-id" not in default_input
    assert "secret-id" in opted_in


def test_scorer_failure_is_separate_from_model_failure():
    _register_eval_components(bad_scorer=True)
    spec = _eval_spec()
    case = EvalCase(id="case-1", input="task", expected={"answer": "yes"})
    result = RunResult(
        status="verify_failed",
        output="invalid",
        run_id="run_failed",
        reason="schema failed",
    )

    scoring = run_scorers(spec, case, result)

    assert scoring.run_status == "verify_failed"
    assert scoring.status == "error"
    assert scoring.scorers[0].error_type == "RuntimeError"
    assert scoring.diagnostics[0].code == "scorer_exception"


def test_scorers_accept_the_documented_convenience_return_values():
    _register_eval_components()
    api = ext.ExtensionAPI(source="test:scorer-return-values")
    metric = RunMetric(
        run_id="run_return_values",
        name="convenience_metric",
        value=1.0,
        direction="maximize",
        unit="ratio",
        source="fixture",
    )
    api.register_scorer(
        "none_scorer", lambda _params, _ctx: lambda _context: None,
        description="Returns no metrics.",
    )
    api.register_scorer(
        "metric_scorer", lambda _params, _ctx: lambda _context: metric,
        description="Returns one metric directly.",
    )
    api.register_scorer(
        "mapping_scorer", lambda _params, _ctx: lambda _context: {"metrics": []},
        description="Returns an output mapping.",
    )
    spec = EvalSpec(
        harness="./h",
        dataset={"loader": "fake_cases"},
        scorers=["none_scorer", "metric_scorer", "mapping_scorer"],
    )

    scoring = run_scorers(
        spec,
        EvalCase(id="case", input="input"),
        RunResult(status="success", output="yes", run_id="run_return_values"),
    )

    assert scoring.status == "success"
    assert [receipt.metric_count for receipt in scoring.scorers] == [0, 1, 0]
    assert scoring.metrics == [metric]


def test_dataset_and_scorer_digests_both_contribute_to_eval_identity():
    _register_eval_components()
    spec = _eval_spec()
    cases = load_eval_cases(spec, ".")
    baseline = eval_identity(spec, cases)
    changed_cases = [cases[0].model_copy(update={"expected": {"answer": "no"}})]
    changed_dataset = eval_identity(spec, changed_cases)
    changed_scorer = eval_identity(_eval_spec(threshold=0.5), cases)

    assert changed_dataset.dataset_digest != baseline.dataset_digest
    assert changed_dataset.eval_id != baseline.eval_id
    assert changed_scorer.scorer_digest != baseline.scorer_digest
    assert changed_scorer.eval_id != baseline.eval_id


def test_eval_loader_trust_gates_local_extensions(monkeypatch, tmp_path: Path):
    extension = tmp_path / "eval_extension.py"
    extension.write_text(
        "def hiveloom_extension(hive):\n"
        "    raise AssertionError('must not execute before trust')\n",
        encoding="utf-8",
    )
    eval_file = tmp_path / "eval.yaml"
    eval_file.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "harness": "./h",
                "extensions": ["eval_extension.py"],
                "dataset": {"loader": "missing"},
                "scorers": ["missing"],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HIVELOOM_TRUST", "never")

    with pytest.raises(SpecError, match="not trusted"):
        load_eval_spec(eval_file)


@pytest.mark.parametrize(
    ("name", "factory", "message"),
    [
        ("empty_eval_cases", lambda: [], "returned no cases"),
        (
            "duplicate_eval_cases",
            lambda: [
                {"id": "duplicate", "input": "first"},
                {"id": "duplicate", "input": "second"},
            ],
            "duplicate case ids",
        ),
        ("invalid_eval_cases", lambda: 42, "must return an iterable"),
    ],
)
def test_eval_loader_reports_invalid_dataset_shapes(
    name: str, factory, message: str
):
    _register_eval_components()
    api = ext.ExtensionAPI(source="test:invalid-eval-datasets")
    api.register_dataset(
        name, lambda _params, _ctx: factory, description="Invalid test dataset."
    )
    spec = EvalSpec(
        harness="./h", dataset={"loader": name}, scorers=["fake_exact"]
    )

    with pytest.raises(SpecError, match=message):
        load_eval_cases(spec, ".")


def test_eval_cli_schema_catalog_and_validate_hide_case_data(tmp_path: Path):
    _register_eval_components()
    harness = tmp_path / "harness"
    construct.init_harness(harness, name="eval-harness", task="Synthetic task.")
    eval_file = tmp_path / "eval.yaml"
    eval_file.write_text(
        yaml.safe_dump(_eval_spec().model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )

    schema = cli.invoke(app, ["eval", "schema", "--json"])
    scorer_catalog = cli.invoke(app, ["catalog", "scorers", "--json"])
    validated = cli.invoke(app, ["eval", "validate", str(eval_file), "--json"])

    assert schema.exit_code == ExitCode.OK
    assert json.loads(schema.stdout)["schema"]["title"] == "EvalSpec"
    assert scorer_catalog.exit_code == ExitCode.OK
    assert json.loads(scorer_catalog.stdout)["entries"][0]["name"] == "fake_exact"
    assert validated.exit_code == ExitCode.OK
    payload = json.loads(validated.stdout)
    assert payload["case_count"] == 1
    assert "public task" not in validated.stdout
    assert '"answer"' not in validated.stdout
