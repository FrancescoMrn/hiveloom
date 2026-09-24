"""Signal location: exact statistics, run features, and the located signal map."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hiveloom import cli, construct
from hiveloom.evolve import stats
from hiveloom.evolve.analyzer import analyze
from hiveloom.evolve.signal import locate_signal
from hiveloom.logging.hive import FEATURES_INDEXED, Hive, derive_features
from hiveloom.logging.trace import spec_version_hash
from hiveloom.spec.loader import load_spec
from hiveloom.spec.schema import EvolutionConfig, TraceExcerptConfig

cli_runner = CliRunner()


def write_run(
    trace_dir: Path,
    run_id: str,
    *,
    status: str = "success",
    tools: list[str] = (),
    tool_errors: list[str] = (),
    version: str = "v1",
    name: str = "h",
    harness_id: str = "",
    task: str = "do the thing",
    verifier_failed: bool = False,
    steps: list[dict] | None = None,
    extra: list[tuple[str, dict]] = (),
    finished_at: str = "2026-01-01T00:00:09+00:00",
) -> Path:
    """A well-formed journal with the events signal location reads."""
    events: list[tuple[str, dict]] = [("run_started", {"input": task})]
    for tool in tools:
        events.append(("tool_call", {"id": tool, "name": tool, "input": {}}))
        events.append(
            ("tool_result", {"id": tool, "name": tool, "is_error": tool in tool_errors,
                             "content": "x"})
        )
    events.extend(extra)
    if verifier_failed:
        events.append(
            ("verification_result", {"verifier": "check", "passed": False, "feedback": "wrong"})
        )
    events.append(
        (
            "run_finished",
            {
                "status": status,
                "turns": 3,
                "cost_usd": 0.01,
                "duration_seconds": 1.0,
                "reason": "",
                "steps": steps or [],
                "execution": {"effective_model": "exec-model"},
            },
        )
    )
    lines = []
    for seq, (etype, payload) in enumerate(events):
        ts = finished_at if etype == "run_finished" else "2026-01-01T00:00:00+00:00"
        lines.append(
            json.dumps(
                {
                    "run_id": run_id,
                    "harness_name": name,
                    "harness_id": harness_id,
                    "harness_version_hash": version,
                    "seq": seq,
                    "timestamp": ts,
                    "type": etype,
                    "payload": payload,
                }
            )
        )
    trace_dir.mkdir(parents=True, exist_ok=True)
    path = trace_dir / f"{run_id}.jsonl"
    path.write_text("\n".join(lines) + "\n")
    return path


def _population(tmp_path: Path, *, failing: int, passing: int) -> Hive:
    """Failures call a flaky http_get that errors; successes also call `check`."""
    traces = tmp_path / "traces"
    for i in range(failing):
        write_run(
            traces, f"f{i}", status="verify_failed", tools=["http_get"],
            tool_errors=["http_get"], verifier_failed=True,
            finished_at=f"2026-01-01T00:{i:02d}:00+00:00",
        )
    for i in range(passing):
        write_run(
            traces, f"s{i}", tools=["http_get", "check"],
            finished_at=f"2026-01-01T01:{i:02d}:00+00:00",
        )
    hive = Hive(tmp_path / "hive.db")
    hive.ingest_dir(traces)
    return hive


# --------------------------------------------------------------------------- #
# Statistics
# --------------------------------------------------------------------------- #
def test_fisher_exact_matches_reference_values():
    # Reference values from scipy.stats.fisher_exact (two-sided).
    assert stats.fisher_exact(1, 9, 11, 3) == pytest.approx(0.002759, abs=1e-5)
    assert stats.fisher_exact(8, 2, 1, 5) == pytest.approx(0.034965, abs=1e-5)
    assert stats.fisher_exact(3, 3, 3, 3) == pytest.approx(1.0)
    assert stats.fisher_exact(0, 0, 0, 0) == 1.0


def test_binomial_two_sided_is_the_exact_sign_test():
    assert stats.binomial_two_sided(0, 6) == pytest.approx(0.03125)
    assert stats.binomial_two_sided(1, 3) == pytest.approx(1.0)
    assert stats.binomial_two_sided(2, 10) == pytest.approx(0.109375)
    assert stats.binomial_two_sided(0, 0) == 1.0


def test_benjamini_hochberg_keeps_input_order_and_is_monotone():
    q = stats.benjamini_hochberg([0.01, 0.04, 0.03, 0.5])
    assert q == pytest.approx([0.04, 0.0533333, 0.0533333, 0.5], abs=1e-6)


def test_power_helpers_say_how_small_a_sample_is():
    low, high = stats.wilson_interval(10, 20)
    assert low == pytest.approx(0.299, abs=1e-3) and high == pytest.approx(0.701, abs=1e-3)
    # 20 runs per arm cannot see a 10-point change; hundreds can.
    assert stats.detectable_change(0.5, 20) > 0.4
    assert stats.runs_needed(0.5, 0.10) == 393


# --------------------------------------------------------------------------- #
# Features
# --------------------------------------------------------------------------- #
def test_features_name_what_happened_but_not_how_it_ended():
    events = [
        {"type": "run_started", "payload": {"input": "x" * 3000}},
        {"type": "tool_call", "payload": {"name": "http_get"}},
        {"type": "tool_result", "payload": {"name": "http_get", "is_error": True}},
        {"type": "memory_selected", "payload": {"ids": ["cite-sources"]}},
        {
            "type": "run_finished",
            "payload": {
                "execution": {"effective_model": "m1"},
                "steps": [{"id": "read", "status": "failed", "violations": ["skipped"]}],
                "delegations": [{"harness": "peer", "status": "success"}],
            },
        },
    ]
    friction = [
        ("r", 1, "tool_error", None, 0, "http_get", "fp", 0, None, "s"),
        ("r", 2, "loop_limit", None, 0, "max_turns", "fp", 0, None, "s"),
    ]
    features = set(derive_features(events, friction))
    assert {
        FEATURES_INDEXED, "input:long", "tool:http_get", "tool_error:http_get",
        "memory:cite-sources", "model:m1", "step:read:failed", "step_violation:read",
        "delegation:peer:success", "friction:tool_error", "friction:tool_error@http_get",
    } <= features
    # loop_limit restates a max_turns ending: it would explain failure by definition.
    assert not any("loop_limit" in f for f in features)


def test_features_are_indexed_and_cleared_on_reingest(tmp_path):
    traces = tmp_path / "traces"
    path = write_run(traces, "r1", tools=["file_read"])
    with Hive(tmp_path / "hive.db") as hive:
        hive.ingest_trace_file(path)
        hive.ingest_trace_file(path)
        [run] = hive.feature_population("h")
        assert run["indexed"] and "tool:file_read" in run["features"]
        assert FEATURES_INDEXED not in run["features"]


# --------------------------------------------------------------------------- #
# Location
# --------------------------------------------------------------------------- #
def test_locates_the_feature_that_separates_failures(tmp_path):
    with _population(tmp_path, failing=8, passing=8) as hive:
        signal_map = locate_signal(hive, "h", version="v1", evolution=EvolutionConfig())
    assert signal_map.verdict == "actionable"
    by_feature = {s.feature: s for s in signal_map.signals}
    error = by_feature["tool_error:http_get"]
    assert error.direction == "risk" and error.strength == "strong"
    assert (error.failures_with, error.successes_with) == (8, 0)
    assert error.levers[0] == "tools" and error.addressable
    # The friction rows derived from the same errors are the same signal.
    assert "friction:tool_error@http_get" in error.aliases
    assert "friction:tool_error@http_get" not in by_feature
    # The tool the good runs call and the bad runs skip is found as readily.
    check = by_feature["tool:check"]
    assert check.direction == "protective" and check.strength == "strong"
    # A tool every run calls cannot separate anything.
    assert "tool:http_get" not in by_feature
    assert "tool_error:http_get" in signal_map.targets
    assert signal_map.knows_target("status:verify_failed")
    assert signal_map.knows_target("metric:anything")


def test_failures_are_attributed_to_a_loss_class(tmp_path):
    traces = tmp_path / "traces"
    write_run(traces, "a", status="max_turns")
    write_run(traces, "b", status="verify_failed", verifier_failed=True)
    write_run(traces, "c", status="verify_failed", tools=["t"], tool_errors=["t"])
    write_run(traces, "d")
    with Hive(tmp_path / "hive.db") as hive:
        hive.ingest_dir(traces)
        signal_map = locate_signal(hive, "h", version="v1")
    assert signal_map.loss.classes == {"limits": 1, "content": 1, "tooling": 1}
    assert signal_map.loss.addressable_share == pytest.approx(2 / 3)
    assert signal_map.statuses == {"verify_failed": 2, "max_turns": 1}


def test_an_external_failure_label_counts_as_a_failure(tmp_path):
    with _population(tmp_path, failing=3, passing=3) as hive:
        hive.record_outcome("s0", "failure", source="user")
        population = {r["run_id"]: r for r in hive.feature_population("h")}
        assert population["s0"]["failed"]
        signal_map = locate_signal(hive, "h")
    assert signal_map.quality.failures == 4


def test_too_few_runs_is_reported_as_underpowered_not_mined(tmp_path):
    with _population(tmp_path, failing=3, passing=1) as hive:
        signal_map = locate_signal(hive, "h")
    assert signal_map.verdict == "underpowered"
    assert any("detectable" in line for line in signal_map.headline)
    # Nothing to contrast against, but what every failure shares is still a target.
    top = signal_map.failure_features[0]
    assert (top.feature, top.failed_runs, top.share_of_failures) == ("tool_error:http_get", 3, 1.0)
    assert signal_map.knows_target("tool_error:http_get")
    assert any("prevalence is the only evidence" in line for line in signal_map.headline)


def test_no_failures_and_no_runs_have_their_own_verdicts(tmp_path):
    with _population(tmp_path, failing=0, passing=3) as hive:
        assert locate_signal(hive, "h").verdict == "no_failures"
        assert locate_signal(hive, "nobody").verdict == "no_runs"


def test_a_frozen_lever_is_reported_out_of_reach(tmp_path):
    frozen_tools = EvolutionConfig(mutable=["system_prompt"])
    with _population(tmp_path, failing=6, passing=6) as hive:
        signal_map = locate_signal(hive, "h", evolution=frozen_tools)
    error = next(s for s in signal_map.signals if s.feature == "tool_error:http_get")
    assert error.levers[0] == "system_prompt" and error.addressable
    model_only = EvolutionConfig(mutable=[])
    with Hive(tmp_path / "hive.db") as hive:
        signal_map = locate_signal(hive, "h", evolution=model_only)
    assert not any(s.addressable for s in signal_map.signals)


def test_runs_ingested_before_features_are_counted_as_unindexed(tmp_path):
    with _population(tmp_path, failing=3, passing=3) as hive:
        hive._conn.execute("DELETE FROM run_features WHERE run_id IN ('f0', 's0')")
        signal_map = locate_signal(hive, "h")
    assert signal_map.quality.unindexed_runs == 2
    assert any("predate feature indexing" in line for line in signal_map.headline)


def test_mechanisms_count_friction_per_component(tmp_path):
    with _population(tmp_path, failing=4, passing=4) as hive:
        signal_map = locate_signal(hive, "h")
    [mechanism] = [m for m in signal_map.mechanisms if m.target == "friction:tool_error@http_get"]
    assert (mechanism.events, mechanism.runs, mechanism.failed_runs) == (4, 4, 4)
    assert mechanism.share_of_failures == 1.0


def test_analyze_carries_the_signal_map_and_successful_examples(tmp_path):
    with _population(tmp_path, failing=5, passing=5) as hive:
        report = analyze(hive, "h", version="v1", evolution=EvolutionConfig())
    assert report.signal_map is not None and report.signal_map.verdict == "actionable"
    assert len(report.recent_successes) == 2
    assert report.recent_successes[0]["run_id"].startswith("s")
    # Task and output are private evidence: only under the trace-excerpt opt-in.
    assert "task" not in report.recent_successes[0]
    with Hive(tmp_path / "hive.db") as hive:
        opted_in = analyze(
            hive, "h", version="v1", excerpt_config=TraceExcerptConfig(enabled=True)
        )
    assert opted_in.recent_successes[0]["task"] == "do the thing"


def test_signal_cli_is_free_and_reads_the_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVELOOM_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HIVELOOM_DB", str(tmp_path / "home" / "hive.db"))
    directory = tmp_path / "h"
    construct.init_harness(directory, name="h", task="Do a thing.")
    spec = load_spec(directory)
    version = spec_version_hash(spec, directory)
    traces = directory / ".hiveloom" / "traces"
    for i in range(4):
        write_run(traces, f"f{i}", status="verify_failed", tools=["t"], tool_errors=["t"],
                  version=version, name="h", harness_id=spec.id)
        write_run(traces, f"s{i}", tools=["t"], version=version, name="h",
                  harness_id=spec.id)
    result = cli_runner.invoke(cli.app, ["signal", str(directory), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] and payload["version"] == version
    assert payload["signals"][0]["feature"] == "tool_error:t"
    plain = cli_runner.invoke(cli.app, ["signal", str(directory)])
    assert plain.exit_code == 0 and "tool_error:t" in plain.output


# --------------------------------------------------------------------------- #
# Aimed proposals
# --------------------------------------------------------------------------- #
def _aimed(target: dict | None, **extra) -> str:
    body = {"rationale": "retry the fetch", **extra,
            "yaml_changes": [{"path": "system_prompt", "value": "Retry a failed fetch."}]}
    if target is not None:
        body["target"] = target
    return json.dumps(body)


def test_the_prompt_carries_the_signal_map_as_its_own_section(tmp_path):
    from hiveloom.evolve.evolver import build_evolve_prompt
    from hiveloom.spec.schema import HarnessSpec

    with _population(tmp_path, failing=6, passing=6) as hive:
        report = analyze(hive, "h", version="v1", evolution=EvolutionConfig())
    spec = HarnessSpec(name="h", description="d", system_prompt="p")
    system, user = build_evolve_prompt(spec, report)
    assert "Locate, then aim" in system
    section = user.split("<signal_map>")[1].split("</signal_map>")[0]
    assert "verdict: actionable" in section
    assert "tool_error:http_get (same runs as: friction:tool_error@http_get" in section
    assert '"tool_error:http_get"' in section.split("targets:")[1]
    # Rendered once, not again inside the raw report JSON.
    assert '"signal_map"' not in user


def test_a_proposal_without_a_known_target_is_sent_back(tmp_path):
    from hiveloom.evolve.evolver import ProposalError, propose
    from hiveloom.generate.llm import FakeStrongModel
    from hiveloom.spec.schema import HarnessSpec

    with _population(tmp_path, failing=6, passing=6) as hive:
        report = analyze(hive, "h", version="v1", evolution=EvolutionConfig())
    spec = HarnessSpec(name="h", description="d", system_prompt="p")
    model = FakeStrongModel(
        [
            _aimed(None),
            _aimed({"signal": "tool_error:nonexistent", "expect": "decrease"}),
            _aimed({"signal": "tool_error:http_get", "expect": "decrease", "by": 0.5}),
        ]
    )
    proposal = propose(spec, report, model)
    assert proposal.target.signal == "tool_error:http_get"
    assert "has no `target`" in model.prompts[1]["user"]
    assert "'tool_error:nonexistent' is not in the signal map" in model.prompts[2]["user"]
    # A metric target must be a configured objective.
    with pytest.raises(ProposalError, match="not a configured evolution objective"):
        propose(spec, report, FakeStrongModel(
            [_aimed({"signal": "metric:quality", "expect": "increase"})] * 3
        ))
    # An aliased name of the same signal is accepted as written.
    alias = propose(spec, report, FakeStrongModel(
        [_aimed({"signal": "friction:tool_error@http_get", "expect": "decrease"})]
    ))
    assert alias.target.signal == "friction:tool_error@http_get"
