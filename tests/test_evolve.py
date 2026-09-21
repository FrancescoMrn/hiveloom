"""Tests for the analyzer and evolver (including frozen-path gating)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# Reuse the Hive trace-writing helper from the Hive tests (tests dir is on sys.path).
from test_hive import _write_trace
from typer.testing import CliRunner

from hiveloom import cli, construct
from hiveloom.errors import ExitCode, SpecError
from hiveloom.evolve import evolver as evolver_mod
from hiveloom.evolve.analyzer import FailureCluster, FailureReport, analyze
from hiveloom.evolve.evolver import (
    CodeChange,
    MutationProposal,
    ProposalError,
    YamlChange,
    apply_proposal,
    build_evolve_prompt,
    gate,
    parse_proposal,
    preview_yaml_changes,
    propose,
)
from hiveloom.generate.llm import FakeStrongModel
from hiveloom.logging.hive import Hive
from hiveloom.logging.trace import spec_version_hash
from hiveloom.spec.loader import load_spec
from hiveloom.spec.schema import HarnessSpec

cli_runner = CliRunner()


def _harness(tmp_path: Path) -> Path:
    directory = tmp_path / "h"
    construct.init_harness(directory, name="demo", task="Do a thing.")
    return directory


def _spec_for_prompt() -> HarnessSpec:
    """A minimal valid spec; these tests are about the prompt, not the harness."""
    return HarnessSpec(
        name="demo", description="Do a thing.", system_prompt="Do the thing."
    )


def _report() -> FailureReport:
    return FailureReport(
        harness_name="demo",
        total_runs=5,
        success_rate=0.2,
        clusters=[FailureCluster(kind="verdict", signature="not valid JSON", count=4)],
    )


# --------------------------------------------------------------------------- #
# Analyzer
# --------------------------------------------------------------------------- #
def test_analyze_builds_report_from_hive(tmp_path: Path):
    _write_trace(tmp_path, "run_a", name="demo", status="verify_failed",
                 verifications=[(False, "not valid JSON")])
    _write_trace(tmp_path, "run_b", name="demo", status="success")
    with Hive(tmp_path / "hive.db") as hive:
        hive.ingest_dir(tmp_path)
        report = analyze(hive, "demo")
    assert report.total_runs == 2
    assert report.success_rate == 0.5
    assert any(c.kind == "verdict" and "JSON" in c.signature for c in report.clusters)


def test_analyze_scopes_counts_clusters_and_examples_to_one_version(tmp_path: Path):
    """Scoping is all-or-nothing: a report whose totals exclude a version but
    whose clusters or examples come from it would mislead the proposing model."""
    _write_trace(tmp_path, "old_fail", name="demo", version="v1", status="verify_failed",
                 verifications=[(False, "not valid JSON")])
    _write_trace(tmp_path, "new_fail", name="demo", version="v2", status="verify_failed",
                 verifications=[(False, "headings are wrong")])
    _write_trace(tmp_path, "new_ok", name="demo", version="v2", status="success")
    with Hive(tmp_path / "hive.db") as hive:
        hive.ingest_dir(tmp_path)
        pooled = analyze(hive, "demo")
        scoped = analyze(hive, "demo", version="v2")

    assert pooled.total_runs == 3
    assert len(pooled.clusters) == 4  # two verdicts + status + indexed friction
    assert scoped.total_runs == 2
    assert scoped.success_rate == 0.5
    assert [c.signature for c in scoped.clusters if c.kind == "verdict"] == ["headings are wrong"]
    assert [c.signature for c in scoped.clusters if c.kind == "friction"] == [
        "verifier_failure"
    ]
    assert [f["run_id"] for f in scoped.recent_failures] == ["new_fail"]


def test_analyze_reports_no_runs_for_an_unrecorded_version(tmp_path: Path):
    """A version with no runs yet reports zeroes, not another version's stats."""
    _write_trace(tmp_path, "old_fail", name="demo", version="v1", status="verify_failed",
                 verifications=[(False, "not valid JSON")])
    with Hive(tmp_path / "hive.db") as hive:
        hive.ingest_dir(tmp_path)
        report = analyze(hive, "demo", version="v9")

    assert report.is_empty()
    assert (report.total_runs, report.success_rate) == (0, 0.0)


def test_analyze_empty_when_no_failures(tmp_path: Path):
    with Hive(tmp_path / "hive.db") as hive:
        report = analyze(hive, "unknown")
    assert report.is_empty()


# --------------------------------------------------------------------------- #
# Gate (safety invariants)
# --------------------------------------------------------------------------- #
def test_gate_rejects_frozen_paths(tmp_path: Path):
    spec = load_spec(_harness(tmp_path))
    proposal = MutationProposal(
        yaml_changes=[
            {"path": "system_prompt", "value": "new"},
            {"path": "guardrails", "value": []},  # frozen
            {"path": "model.id", "value": "claude-opus-4-8"},  # frozen
            {"path": "logging.redact", "value": []},  # always-frozen
            {"path": "hooks", "value": []},  # safety boundary
        ]
    )
    result = gate(spec, proposal)
    accepted = {c.path for c in result.accepted}
    rejected = {r["path"] for r in result.rejected}
    assert accepted == {"system_prompt"}
    assert rejected == {"guardrails", "model.id", "logging.redact", "hooks"}


def test_gate_rejects_parent_of_frozen_leaf(tmp_path: Path):
    """Writing a parent mapping overwrites its frozen child, so it must be
    rejected too: `logging` replaces the frozen `logging.redact`, `evolution`
    replaces the frozen `evolution.auto_propose`. Pre-fix (`_covered`, which
    matches only equality or descendants) these slipped through and silently
    defeated the freeze."""
    spec = load_spec(_harness(tmp_path))
    proposal = MutationProposal(
        yaml_changes=[
            {"path": "logging", "value": {"redact": []}},
            {"path": "evolution", "value": {"auto_propose": {"enabled": True}}},
        ]
    )
    result = gate(spec, proposal)
    assert not result.accepted
    assert {r["path"] for r in result.rejected} == {"logging", "evolution"}
    assert all(r["reason"] == "frozen path" for r in result.rejected)


def test_gate_rejects_dangerous_tool_changes(tmp_path: Path):
    spec = load_spec(_harness(tmp_path))
    proposal = MutationProposal(yaml_changes=[{"path": "tools", "value": [{"builtin": "shell"}]}])

    result = gate(spec, proposal)

    assert not result.accepted
    assert result.rejected[0]["reason"] == (
        "dangerous tool changes require an explicit construct command"
    )


def test_gate_rejects_mcp_servers_regardless_of_harness_mutable_list(tmp_path: Path):
    """ALWAYS_FROZEN must win even if a harness declares mcp_servers mutable."""
    directory = _harness(tmp_path)
    construct.set_field(directory, "evolution.mutable", '["mcp_servers"]')
    spec = load_spec(directory)
    assert "mcp_servers" in spec.evolution.mutable
    assert "mcp_servers" not in spec.evolution.frozen

    proposal = MutationProposal(
        yaml_changes=[{"path": "mcp_servers", "value": [{"name": "x", "command": "y"}]}]
    )
    result = gate(spec, proposal)
    assert not result.accepted
    assert result.rejected == [{"path": "mcp_servers", "reason": "frozen path"}]


def test_gate_rejects_non_mutable_path(tmp_path: Path):
    spec = load_spec(_harness(tmp_path))
    proposal = MutationProposal(yaml_changes=[{"path": "verify.on_fail.max_retries", "value": 9}])
    result = gate(spec, proposal)
    assert not result.accepted
    assert result.rejected[0]["reason"] == "not in the mutable set"


def test_gate_rejects_loop_steps_by_default(tmp_path: Path):
    # loop.steps rewrites what the harness does, and in what order — a bigger
    # behavioral mutation than tuning max_turns or the system prompt — so it
    # is deliberately excluded from the default mutable set.
    spec = load_spec(_harness(tmp_path))
    proposal = MutationProposal(yaml_changes=[{"path": "loop.steps", "value": ["a", "b"]}])
    result = gate(spec, proposal)
    assert not result.accepted
    assert result.rejected[0]["reason"] == "not in the mutable set"


def test_gate_accepts_loop_steps_when_harness_opts_in(tmp_path: Path):
    harness = _harness(tmp_path)
    construct.set_field(
        harness,
        "evolution.mutable",
        '[system_prompt, loop.max_turns, loop.policy, context.strategy, tools, loop.steps]',
    )
    spec = load_spec(harness)
    proposal = MutationProposal(yaml_changes=[{"path": "loop.steps", "value": ["a", "b"]}])
    result = gate(spec, proposal)
    assert {c.path for c in result.accepted} == {"loop.steps"}


def test_gate_rejects_an_accepted_batch_that_would_invalidate_the_spec(tmp_path: Path):
    spec = load_spec(_harness(tmp_path))
    proposal = MutationProposal(
        yaml_changes=[
            {"path": "loop.policy", "value": "sequential_steps"},
            {"path": "guardrails", "value": []},
        ]
    )

    result = gate(spec, proposal)

    assert result.accepted == []
    reasons = {rejection["path"]: rejection["reason"] for rejection in result.rejected}
    assert reasons["guardrails"] == "frozen path"
    assert "invalid spec" in reasons["loop.policy"]
    assert "requires a non-empty loop.steps" in reasons["loop.policy"]


def test_gate_validates_and_accepts_a_valid_multi_change_batch(
    tmp_path: Path, monkeypatch
):
    harness = _harness(tmp_path)
    construct.set_value(
        harness,
        "evolution.mutable",
        ["loop.policy", "loop.steps"],
    )
    spec = load_spec(harness)
    proposal = MutationProposal(
        yaml_changes=[
            {"path": "loop.policy", "value": "sequential_steps"},
            {"path": "loop.steps", "value": ["extract", "verify"]},
        ]
    )
    validated: list[dict] = []
    real_spec_from_dict = evolver_mod.spec_from_dict

    def recording_spec_from_dict(data, *args, **kwargs):
        validated.append(data)
        return real_spec_from_dict(data, *args, **kwargs)

    monkeypatch.setattr(evolver_mod, "spec_from_dict", recording_spec_from_dict)

    result = gate(spec, proposal)

    assert [change.path for change in result.accepted] == [
        "loop.policy",
        "loop.steps",
    ]
    assert result.rejected == []
    assert len(validated) == 1
    assert validated[0]["loop"]["policy"] == "sequential_steps"
    assert validated[0]["loop"]["steps"] == ["extract", "verify"]


def test_gate_rejects_evolution_auto_propose_touching(tmp_path: Path):
    """Evolution must not tune its own auto-propose trigger. It's in
    ALWAYS_FROZEN (fix-round-4 regression), so this is rejected as a frozen
    path — a stronger guarantee than merely being absent from the default
    `mutable` list, which a harness could otherwise override (see below).
    """
    spec = load_spec(_harness(tmp_path))
    proposal = MutationProposal(
        yaml_changes=[{"path": "evolution.auto_propose.enabled", "value": True}]
    )
    result = gate(spec, proposal)
    assert not result.accepted
    assert result.rejected[0]["reason"] == "frozen path"


def test_gate_rejects_evolution_auto_propose_even_with_custom_mutable_list(tmp_path: Path):
    """A harness must not be able to enable its own auto_propose trigger by
    explicitly listing it in a CUSTOM `evolution.mutable` — ALWAYS_FROZEN is
    checked before the mutable set, so this can't be overridden per harness.
    """
    harness = _harness(tmp_path)
    construct.set_value(harness, "evolution.mutable", ["evolution.auto_propose"])
    spec = load_spec(harness)
    proposal = MutationProposal(
        yaml_changes=[{"path": "evolution.auto_propose.enabled", "value": True}]
    )
    result = gate(spec, proposal)
    assert not result.accepted
    assert result.rejected[0]["reason"] == "frozen path"


def test_gate_rejects_case_variant_frozen_paths(tmp_path: Path):
    """Fix-round-5 regression: `_covered`'s comparison must be
    case-insensitive — a case-variant path like `"Model"` or
    `"logging.Redact"` must be rejected as frozen on its own, not merely
    because the mismatched-case write that would otherwise follow creates
    an unrecognized key `_commit`'s pydantic validation rejects anyway
    (that's an unrelated backstop, not this check working).
    """
    spec = load_spec(_harness(tmp_path))
    proposal = MutationProposal(
        yaml_changes=[
            {"path": "Model", "value": {}},
            {"path": "logging.Redact", "value": []},
            {"path": "GUARDRAILS", "value": []},
        ]
    )
    result = gate(spec, proposal)
    assert not result.accepted
    assert all(r["reason"] == "frozen path" for r in result.rejected)
    assert len(result.rejected) == 3


def test_gate_rejects_the_memory_budgets_but_accepts_an_entry(tmp_path: Path):
    """Evolution may add a lesson; it may never widen the store that holds it."""
    spec = load_spec(_harness(tmp_path))
    proposal = MutationProposal(
        yaml_changes=[
            {"path": "memory.max_entries", "value": 200},
            {"path": "memory.enabled", "value": False},
            {"path": "memory", "value": {"max_entry_chars": 4000}},
            {
                "path": "memory.entries.0",
                "value": {
                    "id": "iso-dates",
                    "kind": "rule",
                    "title": "Dates in ISO 8601",
                    "content": "Emit dates as YYYY-MM-DD.",
                },
            },
        ]
    )

    result = gate(spec, proposal)

    assert [change.path for change in result.accepted] == ["memory.entries.0"]
    assert {r["path"] for r in result.rejected} == {
        "memory.max_entries",
        "memory.enabled",
        "memory",
    }
    assert all(r["reason"] == "frozen path" for r in result.rejected)


def test_gate_rejects_memory_paths_that_are_not_entries(tmp_path: Path):
    """The positive statement of the same rule, independent of ALWAYS_FROZEN:
    the only thing under `memory` evolution may write is an entry."""
    harness = _harness(tmp_path)
    construct.set_field(harness, "evolution.mutable", '["memory"]')
    spec = load_spec(harness)

    result = gate(spec, MutationProposal(yaml_changes=[{"path": "memory.notes", "value": 1}]))

    assert not result.accepted
    assert result.rejected[0]["reason"] == (
        "only memory.entries is evolvable; the memory budgets are frozen"
    )


def test_applying_a_memory_entry_appends_at_the_list_length(tmp_path: Path):
    harness = _harness(tmp_path)
    construct.add_memory_entry(harness, kind="fact", title="Nulls", content="Null is null.")

    result = apply_proposal(
        harness,
        MutationProposal(
            yaml_changes=[
                {
                    "path": "memory.entries.1",
                    "value": {
                        "id": "iso-dates",
                        "kind": "rule",
                        "title": "Dates in ISO 8601",
                        "content": "Emit dates as YYYY-MM-DD.",
                        "source": "evolve",
                    },
                }
            ]
        ),
        apply_yaml=True,
    )

    assert result.changed is True
    entries = load_spec(harness).memory.entries
    assert [entry.id for entry in entries] == ["nulls", "iso-dates"]
    assert "# Memory" in _spec_system_prompt(harness)


def test_applying_the_first_memory_entry_creates_the_list(tmp_path: Path):
    """A harness that has learned nothing omits the section entirely, so the
    first append has to create a list — not a mapping with a "0" key."""
    harness = _harness(tmp_path)
    assert "memory:" not in (harness / "harness.yaml").read_text()

    result = apply_proposal(
        harness,
        MutationProposal(
            yaml_changes=[
                {
                    "path": "memory.entries.0",
                    "value": {
                        "id": "iso-dates",
                        "kind": "rule",
                        "title": "Dates in ISO 8601",
                        "content": "Emit dates as YYYY-MM-DD.",
                    },
                }
            ]
        ),
    )

    assert result.changed is True
    assert [entry.id for entry in load_spec(harness).memory.entries] == ["iso-dates"]


def test_an_over_budget_memory_entry_is_rejected_and_nothing_is_written(tmp_path: Path):
    harness = _harness(tmp_path)
    construct.set_value(harness, "memory.prompt_budget_chars", 200)
    before = (harness / "harness.yaml").read_text()

    result = apply_proposal(
        harness,
        MutationProposal(
            yaml_changes=[
                {
                    "path": "memory.entries.0",
                    "value": {
                        "id": "too-long",
                        "kind": "fact",
                        "title": "Too long",
                        "content": "x" * 400,
                    },
                }
            ]
        ),
        apply_yaml=True,
    )

    assert result.changed is False
    assert "prompt_budget_chars" in result.rejected[0]["reason"]
    assert (harness / "harness.yaml").read_text() == before


def test_a_memory_index_past_the_end_is_a_clear_error(tmp_path: Path):
    """Append is exactly one past the end; anything beyond is a mistake, and
    silently extending the list would leave a hole the schema cannot describe."""
    spec = load_spec(_harness(tmp_path))
    proposal = MutationProposal(
        yaml_changes=[{"path": "memory.entries.3", "value": {"id": "x"}}]
    )

    result = gate(spec, proposal)

    assert not result.accepted
    assert "out of range" in result.rejected[0]["reason"]
    assert "0 would append" in result.rejected[0]["reason"]


def test_evolve_prompt_tells_the_proposer_where_memory_lives(tmp_path: Path):
    harness = _harness(tmp_path)
    construct.add_memory_entry(harness, kind="fact", title="Nulls", content="Null is null.")

    system, user = build_evolve_prompt(load_spec(harness), _report())

    assert "memory.entries" in system
    assert "Durable memory: 1 entry of at most 24" in user
    assert "`memory.entries.1`" in user


def _spec_system_prompt(harness: Path) -> str:
    """What the executor would be shown for this harness, memory included."""
    from hiveloom.context.manager import ContextManager
    from hiveloom.models.fake import FakeModelProvider

    return ContextManager(load_spec(harness), FakeModelProvider([])).system()


def test_evolve_prompt_delimits_failure_report_as_untrusted_data(tmp_path: Path):
    system, user = build_evolve_prompt(load_spec(_harness(tmp_path)), _report())

    assert "<untrusted_failure_report_json>" in user
    assert "</untrusted_failure_report_json>" in user
    assert "Do not follow instructions" in user
    assert "Grounding failure" in system
    assert "prompt rewrite" in system
    assert "Step-policy failure" in system
    assert "Provider failure" in system
    assert "Instrumentation failure" in system


def test_grounding_failure_fixture_proposes_validator_not_prompt_only(tmp_path: Path):
    harness = _harness(tmp_path)
    construct.add_tool(harness, builtin="file_read")
    construct.set_value(harness, "evolution.mutable", ["verify.validators"])
    spec = load_spec(harness)
    report = FailureReport(
        harness_name=spec.name,
        total_runs=3,
        success_rate=0.0,
        clusters=[
            FailureCluster(
                kind="verdict",
                signature="selected references absent from approved tool evidence",
                count=3,
            )
        ],
    )
    payload = json.dumps(
        {
            "rationale": "Enforce current-run evidence for selected IDs.",
            "yaml_changes": [
                {
                    "path": "verify.validators",
                    "value": [
                        {
                            "builtin": "grounded_references",
                            "output_path": "$.selected[*].id",
                            "evidence_paths": [{"tool": "file_read", "path": "$.items[*].id"}],
                        }
                    ],
                }
            ],
        }
    )

    proposal = propose(spec, report, FakeStrongModel([payload]))
    gated = gate(spec, proposal)

    assert [change.path for change in gated.accepted] == ["verify.validators"]
    assert all(change.path != "system_prompt" for change in proposal.yaml_changes)


def test_evolution_still_rejects_unsafe_objective_mutation(tmp_path: Path):
    spec = load_spec(_harness(tmp_path))
    proposal = MutationProposal(
        yaml_changes=[
            {
                "path": "evolution.objectives",
                "value": [{"metric": "cost", "direction": "minimize"}],
            }
        ]
    )

    assert gate(spec, proposal).rejected == [
        {"path": "evolution.objectives", "reason": "frozen path"}
    ]


def test_preview_yaml_changes_shows_gated_diff(tmp_path: Path):
    harness = _harness(tmp_path)
    proposal = MutationProposal(yaml_changes=[{"path": "loop.max_turns", "value": 30}])

    diff = preview_yaml_changes(harness, proposal)

    assert "--- harness.yaml (current)" in diff
    assert "+  max_turns: 30" in diff


# --------------------------------------------------------------------------- #
# Apply
# --------------------------------------------------------------------------- #
def test_apply_applies_mutable_and_bumps_counter(tmp_path: Path):
    harness = _harness(tmp_path)
    proposal = MutationProposal(
        rationale="clarify",
        yaml_changes=[
            {"path": "system_prompt", "value": "Output ONLY JSON."},
            {"path": "loop.max_turns", "value": 25},
            {"path": "guardrails", "value": []},  # must be rejected, never applied
        ],
    )
    result = apply_proposal(harness, proposal, apply_yaml=True)
    assert result.changed and result.counter == 1
    assert {r["path"] for r in result.rejected} == {"guardrails"}

    spec = load_spec(harness)
    assert spec.system_prompt == "Output ONLY JSON."
    assert spec.loop.max_turns == 25
    # Guardrails invariant: the cost guardrail is still present, not wiped.
    assert any(getattr(g, "builtin", None) == "max_cost_usd" for g in spec.guardrails)
    assert (harness / "harness.yaml").read_text().startswith("# evolved: 1")


def test_apply_no_changes_when_all_rejected(tmp_path: Path):
    harness = _harness(tmp_path)
    proposal = MutationProposal(yaml_changes=[{"path": "model.id", "value": "x"}])
    result = apply_proposal(harness, proposal, apply_yaml=True)
    assert result.changed is False
    assert result.counter == 0


def test_apply_code_change_requires_approval(tmp_path: Path):
    harness = _harness(tmp_path)
    construct.add_validator(harness, code="validators/check.py:validate")
    proposal = MutationProposal(
        code_changes=[
            {
                "file": "validators/check.py",
                "source": "def validate(run_output, run_context):\n    return {'passed': True}\n",
                "rationale": "fix logic",
            }
        ]
    )
    # Default (no approver) => code is pending, not applied.
    pending = apply_proposal(harness, proposal)
    assert pending.applied_code == [] and pending.pending_code == ["validators/check.py"]
    assert pending.changed is False

    # With approval => applied and a .bak is kept.
    applied = apply_proposal(harness, proposal, approve_code=lambda c: True)
    assert applied.applied_code == ["validators/check.py"]
    assert (harness / "validators" / "check.py.bak").exists()
    assert "return {'passed': True}" in (harness / "validators" / "check.py").read_text()


def test_apply_rolls_back_when_validation_fails(tmp_path: Path, monkeypatch):
    """Writing has to precede validation, because validation imports the code
    changes. A failure there must therefore undo the writes, or the harness is
    left mutated and invalid — the one path where that guarantee was missing.
    """
    harness = _harness(tmp_path)
    yaml_before = (harness / "harness.yaml").read_text()
    proposal = MutationProposal(
        yaml_changes=[YamlChange(path="loop.max_turns", value=9)],
        code_changes=[CodeChange(file="validators/new.py", source="# fresh\n")],
    )

    def boom(_path):
        raise SpecError("hook failed to import")

    monkeypatch.setattr(evolver_mod, "validate_harness", boom)
    with pytest.raises(SpecError, match="hook failed to import"):
        apply_proposal(harness, proposal, apply_yaml=True, approve_code=lambda c: True)

    assert (harness / "harness.yaml").read_text() == yaml_before, "spec must be restored"
    assert not (harness / "validators" / "new.py").exists(), "new file must be removed"
    assert not (harness / "validators" / "new.py.bak").exists(), "no misleading .bak"


def test_apply_rollback_restores_an_overwritten_file(tmp_path: Path, monkeypatch):
    """The other half: a file that already existed goes back to its old body."""
    harness = _harness(tmp_path)
    target = harness / "validators" / "check.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# original\n")

    monkeypatch.setattr(
        evolver_mod, "validate_harness", lambda _p: (_ for _ in ()).throw(SpecError("nope"))
    )
    with pytest.raises(SpecError):
        apply_proposal(
            harness,
            MutationProposal(
                yaml_changes=[YamlChange(path="loop.max_turns", value=9)],
                code_changes=[CodeChange(file="validators/check.py", source="# replaced\n")],
            ),
            apply_yaml=True,
            approve_code=lambda c: True,
        )

    assert target.read_text() == "# original\n"


def test_apply_code_change_cannot_escape_harness(tmp_path: Path):
    harness = _harness(tmp_path)
    outside = tmp_path / "outside.py"
    proposal = MutationProposal(
        code_changes=[
            {"file": "../outside.py", "source": "raise RuntimeError\n", "rationale": "bad"}
        ]
    )

    with pytest.raises(ProposalError, match="outside the harness"):
        apply_proposal(harness, proposal, approve_code=lambda _change: True)

    assert not outside.exists()


def test_apply_code_change_refuses_configured_trace_dir(tmp_path: Path):
    """Fix-round-3 regression: a code change may not target the harness's
    OWN (possibly reconfigured, non-default) trace directory either — the
    same protection file_read/file_write and the HTTP control plane's
    input_file get.
    """
    harness = _harness(tmp_path)
    construct.set_value(harness, "logging.trace_dir", "run_logs")
    (harness / "run_logs").mkdir()
    proposal = MutationProposal(
        code_changes=[
            {"file": "run_logs/evil.py", "source": "raise RuntimeError\n", "rationale": "bad"}
        ]
    )

    with pytest.raises(ProposalError, match="outside the harness"):
        apply_proposal(harness, proposal, approve_code=lambda _change: True)

    assert not (harness / "run_logs" / "evil.py").exists()


def test_apply_records_evolution_in_hive(tmp_path: Path):
    harness = _harness(tmp_path)
    proposal = MutationProposal(
        rationale="tune", yaml_changes=[{"path": "loop.max_turns", "value": 30}]
    )
    with Hive(tmp_path / "hive.db") as hive:
        result = apply_proposal(harness, proposal, hive=hive, apply_yaml=True)
        evolutions = hive.evolutions(load_spec(harness).identity)
    assert len(evolutions) == 1
    assert evolutions[0]["new_version_hash"] == result.new_version_hash
    assert evolutions[0]["rationale"] == "tune"


def test_propose_parses_model_json(tmp_path: Path):
    spec = load_spec(_harness(tmp_path))
    payload = json.dumps(
        {"rationale": "r", "yaml_changes": [{"path": "loop.policy", "value": "plan_then_act"}]}
    )
    proposal = propose(spec, _report(), FakeStrongModel([payload]))
    assert proposal.yaml_changes[0].path == "loop.policy"


def test_parse_proposal_rejects_bad_json():
    with pytest.raises(Exception, match="not valid JSON"):
        parse_proposal("definitely not json")


def test_parse_proposal_recovers_an_object_narrated_in_prose():
    """A strong model asked to analyse failures usually narrates first and emits
    the object last. Observed against the real evolve prompt: 1kB of markdown
    analysis, then a valid proposal. Rejecting that discards a good proposal
    over its packaging.
    """
    narrated = (
        "Looking at the failure clusters:\n\n"
        "1. **Invalid JSON (479 cases)** is dominant.\n"
        "2. Headings exceed the limit.\n\n"
        "Here is my proposal:\n\n"
        '{"rationale": "tighten output rules",\n'
        ' "yaml_changes": [{"path": "loop.max_turns", "value": 12,'
        ' "rationale": "room to retry"}],\n'
        ' "code_changes": []}\n'
    )
    proposal = parse_proposal(narrated)

    assert proposal.rationale == "tighten output rules"
    assert [c.path for c in proposal.yaml_changes] == ["loop.max_turns"]


def test_parse_proposal_recovers_an_object_with_trailing_commentary():
    """Prose after the object must not defeat the match either."""
    proposal = parse_proposal(
        'Proposal:\n{"rationale": "r", "yaml_changes": [], "code_changes": []}\n'
        "I would also suggest reviewing the validators, though that is out of scope."
    )

    assert proposal.rationale == "r"


def test_parse_proposal_still_rejects_prose_with_no_object():
    with pytest.raises(Exception, match="not valid JSON"):
        parse_proposal("I considered several mutations but recommend none at this time.")


# --------------------------------------------------------------------------- #
# CLI: evolve --propose (queues instead of applying)
# --------------------------------------------------------------------------- #
_PROPOSAL_PAYLOAD = json.dumps(
    {"rationale": "clarify", "yaml_changes": [{"path": "loop.max_turns", "value": 25}]}
)


def _seed_failure(tmp_path: Path, harness: Path) -> None:
    """Ingest one failed run for the harness at ``harness``.

    Name and version hash come from the live spec, as a real run's trace
    carries them: `evolve` scopes its analysis to the current version, so a
    hand-written hash would read as history from a spec that no longer exists.
    """
    spec = load_spec(harness)
    trace = _write_trace(
        tmp_path, "run_a", name=spec.name, harness_id=spec.id,
        version=spec_version_hash(spec, harness),
        status="verify_failed", verifications=[(False, "not valid JSON")],
    )
    with Hive() as hive:  # the autouse conftest fixture points this at a throwaway db
        hive.ingest_trace_file(trace)


def _fake_model(monkeypatch, *responses: str) -> None:
    from hiveloom.generate import llm as llm_mod

    monkeypatch.setattr(
        llm_mod, "build_strong_model", lambda *a, **k: FakeStrongModel(list(responses))
    )


def test_cli_evolve_says_failures_are_from_an_earlier_version(tmp_path: Path, monkeypatch):
    """Editing the harness invalidates its failure history for evolution. Saying
    "no recorded failures" there sends the user hunting a logging bug instead of
    re-running the harness."""
    harness = _harness(tmp_path)
    _seed_failure(tmp_path, harness)
    construct.set_field(harness, "loop.max_turns", "9")  # new spec, new version hash
    _fake_model(monkeypatch, _PROPOSAL_PAYLOAD)

    result = cli_runner.invoke(cli.app, ["evolve", str(harness), "--propose", "--json"])

    assert result.exit_code == ExitCode.OK, result.stdout
    payload = json.loads(result.stdout)
    assert payload["changed"] is False
    assert "1 on earlier versions" in payload["reason"]


def test_cli_evolve_propose_queues_without_applying(tmp_path: Path, monkeypatch):
    harness = _harness(tmp_path)
    _seed_failure(tmp_path, harness)
    _fake_model(monkeypatch, _PROPOSAL_PAYLOAD)

    result = cli_runner.invoke(cli.app, ["evolve", str(harness), "--propose", "--json"])

    assert result.exit_code == ExitCode.OK, result.stdout
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["status"] == "pending"
    assert payload["id"].startswith("prop_")
    assert not (harness / "harness.yaml").read_text().startswith("# evolved")


def test_cli_evolve_resolves_a_harness_declared_provider(tmp_path: Path):
    harness = _harness(tmp_path)
    extension = harness / "evolve_provider.py"
    extension.write_text(
        """
from hiveloom.models.fake import FakeModelProvider, text_response

def hiveloom_extension(hive):
    hive.register_provider(
        "local_evolver",
        lambda _ctx: FakeModelProvider([text_response(
            '{"rationale":"clarify","yaml_changes":'
            '[{"path":"loop.max_turns","value":25}]}'
        )]),
        models=[{"id": "proposal-model", "provider": "local_evolver"}],
        api="local",
    )
""".strip()
        + "\n",
        encoding="utf-8",
    )
    construct.set_value(harness, "extensions", ["evolve_provider.py"])
    _seed_failure(tmp_path, harness)

    result = cli_runner.invoke(
        cli.app,
        [
            "evolve",
            str(harness),
            "--model",
            "local_evolver/proposal-model",
            "--propose",
            "--json",
        ],
    )

    assert result.exit_code == ExitCode.OK, result.stdout
    assert json.loads(result.stdout)["status"] == "pending"


def test_cli_evolve_propose_ignores_yes(tmp_path: Path, monkeypatch):
    harness = _harness(tmp_path)
    _seed_failure(tmp_path, harness)
    _fake_model(monkeypatch, _PROPOSAL_PAYLOAD)

    result = cli_runner.invoke(
        cli.app, ["evolve", str(harness), "--propose", "--yes", "--json"]
    )

    assert result.exit_code == ExitCode.OK, result.stdout
    assert json.loads(result.stdout)["status"] == "pending"
    assert not (harness / "harness.yaml").read_text().startswith("# evolved")


def test_cli_evolve_without_propose_is_unchanged(tmp_path: Path, monkeypatch):
    """Regression: plain `hiveloom evolve <dir>` still applies directly, no queue."""
    harness = _harness(tmp_path)
    _seed_failure(tmp_path, harness)
    _fake_model(monkeypatch, _PROPOSAL_PAYLOAD)

    result = cli_runner.invoke(cli.app, ["evolve", str(harness), "--yes", "--json"])

    assert result.exit_code == ExitCode.OK, result.stdout
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["changed"] is True
    assert (harness / "harness.yaml").read_text().startswith("# evolved: 1")


# --------------------------------------------------------------------------- #
# Evolving a fork: --from-parent
# --------------------------------------------------------------------------- #
def _forked_from_a_failure(tmp_path: Path) -> Path:
    """A real fork of a real failing run, edited so it has a version of its own.

    Built through ``create_fork`` rather than by hand: ``--from-parent`` reads
    a key out of the lineage record, so a stand-in ``fork.yaml`` would keep
    passing if that key were ever renamed.
    """
    import shutil

    from hiveloom import fork as fork_mod
    from hiveloom import runner
    from hiveloom.models.fake import FakeModelProvider, text_response, tool_response

    example = Path(__file__).resolve().parents[1] / "harnesses" / "example-summarizer"
    parent = tmp_path / "summarizer"
    shutil.copytree(example, parent)
    (parent / "notes.txt").write_text("The quick brown fox jumps over the lazy dog. " * 30)

    provider = FakeModelProvider(
        [
            tool_response("file_read", {"path": "notes.txt"}, call_id="c1"),
            text_response("not json"),
            text_response("still not json"),
        ]
    )
    result = runner.run_harness(parent, "notes.txt", provider=provider)
    assert result.status == "verify_failed"

    fork_dir = tmp_path / "probe"
    fork_mod.create_fork(result.trace_path, fork_dir)
    # The edit is the point of forking, and it is also what gives the fork a
    # version hash of its own — which is what hides the parent's failures.
    construct.set_field(fork_dir, "loop.max_turns", "9")
    return fork_dir


def test_a_fresh_fork_has_nothing_to_evolve_without_from_parent(tmp_path: Path, monkeypatch):
    """The gap --from-parent closes: a fork is created *because* its parent
    failed, but it has no runs at its own version, so the default scoping
    reports nothing at exactly the moment there is most to say."""
    fork_dir = _forked_from_a_failure(tmp_path)
    _fake_model(monkeypatch, _PROPOSAL_PAYLOAD)

    result = cli_runner.invoke(cli.app, ["evolve", str(fork_dir), "--propose", "--json"])

    assert result.exit_code == ExitCode.OK, result.stdout
    payload = json.loads(result.stdout)
    assert payload["changed"] is False
    assert "on earlier versions" in payload["reason"]


def test_from_parent_evolves_a_fork_against_the_failure_it_came_from(tmp_path: Path, monkeypatch):
    fork_dir = _forked_from_a_failure(tmp_path)
    _fake_model(monkeypatch, _PROPOSAL_PAYLOAD)

    result = cli_runner.invoke(
        cli.app, ["evolve", str(fork_dir), "--from-parent", "--propose", "--json"]
    )

    assert result.exit_code == ExitCode.OK, result.stdout
    payload = json.loads(result.stdout)
    assert payload["status"] == "pending"
    # Recorded as fork-triggered, so the queue says where the evidence came from.
    assert payload["trigger"] == "fork"
    # Queued, never applied — the gate is unchanged by the new flag.
    assert not (fork_dir / "harness.yaml").read_text().startswith("# evolved")


def test_from_parent_needs_a_fork_directory(tmp_path: Path, monkeypatch):
    harness = _harness(tmp_path)
    _seed_failure(tmp_path, harness)
    _fake_model(monkeypatch, _PROPOSAL_PAYLOAD)

    result = cli_runner.invoke(
        cli.app, ["evolve", str(harness), "--from-parent", "--propose", "--json"]
    )

    assert result.exit_code == ExitCode.SPEC_ERROR
    assert "fork" in json.loads(result.stdout)["error"]


# --------------------------------------------------------------------------- #
# A researcher is a model too: a malformed proposal is feedback, not a dead end
# --------------------------------------------------------------------------- #
def _minimal_proposal_payload() -> str:
    return json.dumps(
        {
            "rationale": "tighten the answer contract",
            "yaml_changes": [{"path": "loop.max_turns", "value": 30}],
        }
    )


def test_a_malformed_proposal_is_retried_with_the_parse_error(tmp_path):
    """Failing the step on the first bad reply throws away the analysis behind it."""
    spec = load_spec(_harness(tmp_path))
    model = FakeStrongModel(
        ["I think we should... (prose, no object)", _minimal_proposal_payload()]
    )

    proposal = propose(spec, _report(), model)

    assert proposal.rationale == "tighten the answer contract"


def test_the_retry_tells_the_researcher_what_was_wrong(tmp_path):
    spec = load_spec(_harness(tmp_path))
    model = FakeStrongModel(["not json at all", _minimal_proposal_payload()])

    propose(spec, _report(), model)

    # The second prompt must carry the failure, or the model repeats itself.
    assert len(model.prompts) == 2
    second = model.prompts[-1]["user"]
    assert "could not be used" in second
    assert "JSON object and nothing else" in second
    # ...and the original analysis is still there, not replaced by the complaint.
    assert model.prompts[0]["user"] in second


def test_a_researcher_that_never_complies_still_fails_loudly(tmp_path):
    spec = load_spec(_harness(tmp_path))
    model = FakeStrongModel(["prose", "more prose", "still prose"])

    with pytest.raises(ProposalError, match="after 3 attempts"):
        propose(spec, _report(), model)


# --------------------------------------------------------------------------- #
# Search memory: what was already tried
# --------------------------------------------------------------------------- #
def test_the_prompt_carries_what_was_already_tried_and_refuted(tmp_path: Path):
    """A memoryless proposer re-proposes the same mutation forever.

    This is the defect that stalled the ARC-AGI-2 autoresearch loop: the
    prompt was the spec plus a failure report, and the driver only advanced its
    evidence pointer on a *keep*. So after a revert the researcher saw
    byte-identical input and produced the same idea again — a fixed point
    wearing the costume of a search.
    """
    from hiveloom.evolve.analyzer import AttemptRecord
    from hiveloom.evolve.evolver import build_evolve_prompt

    spec = load_spec(_harness(tmp_path))
    report = FailureReport(
        harness_name="h",
        total_runs=10,
        success_rate=0.4,
        clusters=[FailureCluster(kind="verdict", signature="bad answer", count=6)],
        attempt_history=[
            AttemptRecord(
                outcome="reverted",
                rationale="clarify the answer format in the system prompt",
                changed_paths=["system_prompt"],
                yaml_diff="-old prompt\n+new prompt\n",
                measured={"tasks_improved": 3, "tasks_regressed": 6, "p_improved": 0.9},
                note="primary regressed",
            )
        ],
    )
    _, user = build_evolve_prompt(spec, report)

    assert "outcome=reverted" in user
    assert "clarify the answer format in the system prompt" in user
    assert "tasks_regressed" in user
    assert "already tried" in user
    # Rendered once, not twice: the history is stripped from the report JSON so
    # a long diff does not spend the prompt budget on both copies.
    assert user.count("clarify the answer format in the system prompt") == 1
    assert '"attempt_history"' not in user


def test_a_history_diff_is_truncated_so_one_attempt_cannot_eat_the_prompt(tmp_path: Path):
    """77k characters of prompt is how the proposer stopped emitting JSON.

    One rewritten system_prompt is thousands of lines of diff; a dozen of them
    crowd out the failures the proposal is supposed to address.
    """
    from hiveloom.evolve.analyzer import AttemptRecord
    from hiveloom.evolve.evolver import build_evolve_prompt

    report = FailureReport(
        harness_name="h",
        total_runs=1,
        success_rate=0.0,
        attempt_history=[
            AttemptRecord(outcome="reverted", yaml_diff="+" + ("x" * 50_000))
        ],
    )
    _, user = build_evolve_prompt(load_spec(_harness(tmp_path)), report)
    assert "(diff truncated)" in user
    assert len(user) < 20_000


def test_an_empty_history_adds_nothing_to_the_prompt(tmp_path: Path):
    from hiveloom.evolve.evolver import build_evolve_prompt

    _, user = build_evolve_prompt(
        load_spec(_harness(tmp_path)),
        FailureReport(harness_name="h", total_runs=1, success_rate=0.0),
    )
    assert "already tried" not in user


# --------------------------------------------------------------------------- #
# The researcher's own output budget
# --------------------------------------------------------------------------- #
def test_a_strong_model_budget_follows_the_declared_ceiling(monkeypatch):
    """A reasoning researcher starved of output tokens narrates and never answers.

    Measured on the ARC-AGI-2 evolve prompt: at the old flat 4096 the researcher
    returned 17,698 characters of reasoning prose and no proposal, three times
    in a row; at 65,536 it returned valid JSON first try. This is the same
    defect the executor had — a flat ceiling far below what the model allows —
    and it gets the same answer: ask the registry.
    """
    from hiveloom import ext
    from hiveloom.generate import llm

    class _Info:
        max_output_tokens = 8192

    # An explicit request always wins.
    assert llm.strong_max_tokens("anything", 123) == 123
    # A declared ceiling below the default caps the ask, so we never request
    # more room than the provider will give (a 400 ends the call outright).
    monkeypatch.setattr(ext, "model_info", lambda _id: _Info())
    assert llm.strong_max_tokens("small-model") == 8192
    # Unknown capabilities retain the compatible historical fallback.
    monkeypatch.setattr(ext, "model_info", lambda _id: None)
    assert llm.strong_max_tokens("unknown") == llm.FALLBACK_STRONG_MAX_TOKENS
    assert llm.DEFAULT_STRONG_MAX_TOKENS > 4096


def test_unbounded_failure_records_cannot_swamp_the_evolve_prompt():
    """Every sibling evidence section has a configured cap; this one had none.

    `recent_failures` carried whole run records — task statement, output, and
    every failed verification — so on a harness with a large task statement
    five of them were 55k characters of a 129k prompt, burying the clusters the
    proposal is supposed to address.
    """
    from hiveloom.evolve.evolver import _MAX_EVIDENCE_STRING_CHARS, build_evolve_prompt

    huge = "G" * 40_000
    report = FailureReport(
        harness_name="h",
        total_runs=5,
        success_rate=0.0,
        clusters=[FailureCluster(kind="verdict", signature="bad answer", count=6)],
        recent_failures=[{"run_id": "r1", "task": huge, "output": huge}],
    )
    _, user = build_evolve_prompt(_spec_for_prompt(), report)

    assert "truncated]" in user
    assert huge not in user
    assert len(user) < 20_000
    # The signal survives the cut: the proposer still sees what went wrong.
    assert "bad answer" in user
    assert "G" * _MAX_EVIDENCE_STRING_CHARS in user


def test_operator_findings_reach_the_proposer_as_trusted_guidance():
    """Evidence built from failures cannot contain an opportunity.

    A harness that samples a task once, and would have been right had it
    sampled three times, produces a clean run with no failure signature at all.
    No amount of better clustering surfaces that, so findings from analysis need
    their own channel — and they are operator-authored, so unlike run data they
    are presented as something to act on.
    """
    from hiveloom.evolve.evolver import build_evolve_prompt

    report = FailureReport(
        harness_name="h",
        total_runs=20,
        success_rate=1.0,
        analyst_notes=[
            "Output formatting is 0% of loss; that work is finished.",
            "Independent samples disagree on 22.2% of pairs.",
        ],
    )
    _, user = build_evolve_prompt(_spec_for_prompt(), report)

    assert "Independent samples disagree on 22.2% of pairs." in user
    assert "trusted" in user
    # Rendered once, in its own section — not buried inside the report JSON.
    assert '"analyst_notes"' not in user
    assert user.count("Output formatting is 0% of loss") == 1


def test_findings_alone_make_a_report_worth_evolving():
    """A harness with no failures can still have work worth doing.

    `is_empty` gates the whole evolve step. Without this, an arm that fails
    nothing — the reproduced ARC-AGI-2 reference, for instance — reports
    "nothing to evolve" even when analysis has found where its remaining loss
    is and how to reach it.
    """
    assert FailureReport(harness_name="h", total_runs=9, success_rate=1.0).is_empty()
    assert not FailureReport(
        harness_name="h", total_runs=9, success_rate=1.0,
        analyst_notes=["wrong_content is 47.9% of loss and sampling is the lever"],
    ).is_empty()
