"""Generation guidance and the synthetic ranked-retrieval example."""

from __future__ import annotations

import shutil
from pathlib import Path

from hiveloom import catalog, guide, runner
from hiveloom.evals import resolve_eval_spec, run_scorers
from hiveloom.models.fake import FakeModelProvider, text_response, tool_response
from hiveloom.spec import annotate
from hiveloom.spec.loader import validate_harness

EXAMPLE = Path(__file__).resolve().parents[1] / "harnesses" / "ranked-retrieval"


def _copy_example(tmp_path: Path) -> Path:
    target = tmp_path / "ranked-retrieval"
    shutil.copytree(EXAMPLE, target, ignore=shutil.ignore_patterns(".hiveloom", "__pycache__"))
    return target


def test_new_workflow_contracts_are_discoverable():
    loop_steps = annotate.explain("loop.steps")
    build_guide = guide.read_topic("build")

    assert "sequential_steps" in catalog.CATALOGS["policies"]
    assert "grounded_references" in catalog.CATALOGS["validators"]
    assert "required successful" in loop_steps["description"]
    assert "deterministic composite tool" in build_guide
    assert "grounded_references" in build_guide


def test_ranked_retrieval_example_validates_and_exposes_enforced_phases(
    tmp_path: Path, monkeypatch
):
    harness = _copy_example(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("HIVELOOM_TRUST", "always")

    spec = validate_harness(harness)
    plan = runner.dry_run(harness, "Rank PostgreSQL performance records.")
    validated_eval, cases = resolve_eval_spec(
        harness / "eval.yaml", approve_trust=lambda _path: True
    )

    assert spec.loop.policy == "sequential_steps"
    assert [step["tools"] for step in plan["steps"]] == [
        ["search_and_verify_records"],
        [],
    ]
    assert plan["steps"][0]["require_tool_calls"] == ["search_and_verify_records"]
    assert validated_eval.case_count == 3
    assert len(cases) == 3


def test_ranked_retrieval_workflow_and_metrics_run_offline(tmp_path: Path, monkeypatch):
    harness = _copy_example(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("HIVELOOM_TRUST", "always")
    validated_eval, cases = resolve_eval_spec(
        harness / "eval.yaml", approve_trust=lambda _path: True
    )
    case = cases[0]
    provider = FakeModelProvider(
        [
            tool_response(
                "search_and_verify_records",
                {"query": case.input, "limit": 5},
                call_id="retrieve",
            ),
            text_response(
                '{"selected":['
                '{"record_id":"record-postgres-indexes","reason":"indexing"},'
                '{"record_id":"record-query-plans","reason":"query plans"},'
                '{"record_id":"record-cache-basics","reason":"cache"}'
                "]}"
            ),
        ]
    )

    result = runner.run_harness(harness, case.input, provider=provider)
    scoring = run_scorers(
        validated_eval.spec,
        case,
        result,
        base_dir=harness,
    )

    assert result.status == "success"
    assert result.turns == 2
    assert [[tool["name"] for tool in call["tools"]] for call in provider.calls] == [
        ["search_and_verify_records"],
        [],
    ]
    assert {metric.name: metric.value for metric in scoring.metrics} == {
        "recall_at_3": 1.0,
        "ndcg_at_3": 1.0,
        "hallucination_rate": 0.0,
    }
