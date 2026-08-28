"""Forking a run: re-entering a finished journal at one of its model calls."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from hiveloom import fork as fork_mod
from hiveloom import runner
from hiveloom.errors import SpecError
from hiveloom.logging.hive import Hive
from hiveloom.logging.journal import read_events, state_at_model_call
from hiveloom.models.fake import FakeModelProvider, text_response, tool_response

EXAMPLE_HARNESS = Path(__file__).resolve().parents[1] / "harnesses" / "example-summarizer"

_VALID_SUMMARY = json.dumps(
    {"title": "Fox", "summary": "A fox jumps a dog.", "key_points": ["fox", "dog"]}
)


def _harness(tmp_path: Path) -> Path:
    target = tmp_path / "summarizer"
    shutil.copytree(EXAMPLE_HARNESS, target)
    (target / "notes.txt").write_text("The quick brown fox jumps over the lazy dog. " * 30)
    return target


def _failing_parent(tmp_path: Path) -> tuple[Path, object]:
    """A run that reads the file, then fails verification twice."""
    harness = _harness(tmp_path)
    provider = FakeModelProvider(
        [
            tool_response("file_read", {"path": "notes.txt"}, call_id="c1"),
            text_response("not json"),
            text_response("still not json"),
        ]
    )
    result = runner.run_harness(harness, "notes.txt", provider=provider, ingest=False)
    assert result.status == "verify_failed"
    return harness, result


# --------------------------------------------------------------------------- #
# Fork points
# --------------------------------------------------------------------------- #
def test_fork_points_are_the_model_calls(tmp_path: Path):
    _, result = _failing_parent(tmp_path)
    points = fork_mod.fork_points(read_events(result.trace_path))

    assert points
    assert [p.turn for p in points] == list(range(len(points)))
    assert all(p.phase == "act" for p in points)


def test_a_non_model_call_seq_snaps_back(tmp_path: Path):
    _, result = _failing_parent(tmp_path)
    points = fork_mod.fork_points(read_events(result.trace_path))

    # One past the first fork point is a model_response, not a call.
    chosen = fork_mod.resolve_fork_point(points, points[0].seq + 1)
    assert chosen.seq == points[0].seq


def test_a_seq_before_the_first_call_is_an_error(tmp_path: Path):
    _, result = _failing_parent(tmp_path)
    points = fork_mod.fork_points(read_events(result.trace_path))

    with pytest.raises(SpecError, match="before the run's first model call"):
        fork_mod.resolve_fork_point(points, 0)


def test_no_at_forks_from_the_last_call(tmp_path: Path):
    _, result = _failing_parent(tmp_path)
    points = fork_mod.fork_points(read_events(result.trace_path))
    assert fork_mod.resolve_fork_point(points, None).seq == points[-1].seq


# --------------------------------------------------------------------------- #
# Materialising
# --------------------------------------------------------------------------- #
def test_fork_materialises_a_runnable_harness(tmp_path: Path):
    harness, result = _failing_parent(tmp_path)
    points = fork_mod.fork_points(read_events(result.trace_path))

    forked = fork_mod.create_fork(result.trace_path, tmp_path / "fork", at=points[-1].seq)

    assert (forked.directory / "harness.yaml").is_file()
    assert (forked.directory / "validators" / "check_summary.py").is_file()
    assert (forked.directory / "fork.yaml").is_file()
    assert forked.parent_run_id == result.run_id
    # The spec comes from the journal, not the folder, so what ran is what got
    # forked. It is re-dumped rather than copied, so compare specs not bytes.
    from hiveloom.spec.loader import load_spec

    assert load_spec(forked.directory / "harness.yaml") == load_spec(
        harness / "harness.yaml"
    )


def test_fork_materialises_playbook_prompt_files(tmp_path: Path):
    harness = _harness(tmp_path)
    from hiveloom import construct

    construct.add_playbook(
        harness,
        name="triage",
        description="Collect evidence.",
        entry=True,
    )
    prompt = harness / "playbooks" / "triage.md"
    prompt.write_text("Read the evidence before deciding.\n", encoding="utf-8")
    result = runner.run_harness(
        harness,
        "notes.txt",
        provider=FakeModelProvider([text_response(_VALID_SUMMARY)]),
        ingest=False,
    )

    forked = fork_mod.create_fork(result.trace_path, tmp_path / "fork")

    copied = forked.directory / "playbooks" / "triage.md"
    assert copied.read_text(encoding="utf-8") == prompt.read_text(encoding="utf-8")
    # Loading the fork exercises the same validation that `run --resume` uses.
    from hiveloom.spec.loader import load_spec

    assert load_spec(forked.directory / "harness.yaml").playbooks[0].name == "triage"


def test_the_seeded_context_is_exactly_what_the_parent_sent(tmp_path: Path):
    _, result = _failing_parent(tmp_path)
    events = read_events(result.trace_path)
    points = fork_mod.fork_points(events)
    at = points[-1].seq

    forked = fork_mod.create_fork(result.trace_path, tmp_path / "fork", at=at)
    seeded = fork_mod.load_fork_context(forked.directory)

    assert seeded == state_at_model_call(events, at).messages


def test_lineage_pins_the_exact_journal_line(tmp_path: Path):
    _, result = _failing_parent(tmp_path)
    points = fork_mod.fork_points(read_events(result.trace_path))
    at = points[-1].seq

    forked = fork_mod.create_fork(result.trace_path, tmp_path / "fork", at=at)
    record = fork_mod.load_fork(forked.directory)

    assert record["parent_run_id"] == result.run_id
    assert record["at_seq"] == at
    assert len(record["parent_line_hash"]) == 64


def test_fork_refuses_a_non_empty_target(tmp_path: Path):
    _, result = _failing_parent(tmp_path)
    target = tmp_path / "fork"
    target.mkdir()
    (target / "something").write_text("x")

    with pytest.raises(SpecError, match="already exists and is not empty"):
        fork_mod.create_fork(result.trace_path, target)


# --------------------------------------------------------------------------- #
# Where a fork lives
# --------------------------------------------------------------------------- #
# A fork is an experiment *on* a harness rather than a harness of its own, so
# it belongs inside that harness's workbench state. `fork_target` is the one
# resolver — CLI, workbench and MCP all go through it — which is what stops
# the three from disagreeing about where a fork goes.
def test_a_fork_lands_inside_the_harness_it_came_from(tmp_path: Path):
    harness = _harness(tmp_path)

    target = fork_mod.fork_target(harness, "probe")

    assert target == harness / ".hiveloom" / "forks" / "probe"


def test_a_fork_of_a_fork_is_a_sibling_not_a_deeper_nest(tmp_path: Path):
    """Depth would record generation, which nobody asked about; fork.yaml does."""
    harness = _harness(tmp_path)
    first = fork_mod.fork_target(harness, "first")
    first.mkdir(parents=True)
    (first / "harness.yaml").write_text("name: x\n")

    assert fork_mod.fork_target(first, "second") == harness / ".hiveloom" / "forks" / "second"
    assert fork_mod.harness_root(first) == harness.resolve()


def test_a_name_that_would_escape_the_harness_is_refused(tmp_path: Path):
    """The caller may be a browser or an MCP client; a name is not a path."""
    harness = _harness(tmp_path)

    for name in ("../escape", "/etc/hiveloom", "a/b", "", "x" * 65):
        with pytest.raises(SpecError, match="fork name"):
            fork_mod.fork_target(harness, name)


def test_a_folder_that_is_not_a_fork_owns_itself(tmp_path: Path):
    harness = _harness(tmp_path)

    assert fork_mod.owning_harness(harness) is None
    assert fork_mod.harness_root(harness) == harness.resolve()


def test_containment_survives_the_fork_being_renamed(tmp_path: Path):
    """Grouping in the workbench follows the folder, not the name inside it."""
    harness, result = _failing_parent(tmp_path)
    forked = fork_mod.create_fork(result.trace_path, fork_mod.fork_target(harness, "probe"))

    from hiveloom import construct

    construct.set_value(forked.directory, "name", "something-else-entirely")

    assert fork_mod.owning_harness(forked.directory) == harness.resolve()


def test_the_cli_puts_a_fork_inside_the_harness_by_default(tmp_path: Path):
    """`--dir` is a developer's own shell; without it, containment holds."""
    from typer.testing import CliRunner

    from hiveloom.cli import app

    harness, result = _failing_parent(tmp_path)

    invoked = CliRunner().invoke(
        app, ["fork", result.run_id, "--name", "probe", "--ingest", str(harness), "--json"]
    )

    assert invoked.exit_code == 0, invoked.stdout
    where = Path(json.loads(invoked.stdout)["directory"])
    assert where == harness / ".hiveloom" / "forks" / "probe"


# --------------------------------------------------------------------------- #
# The guards
# --------------------------------------------------------------------------- #
def test_drift_in_a_harness_file_refuses_the_fork(tmp_path: Path):
    harness, result = _failing_parent(tmp_path)
    (harness / "validators" / "check_summary.py").write_text("# edited after the run\n")

    with pytest.raises(SpecError, match="has changed since the parent run"):
        fork_mod.create_fork(result.trace_path, tmp_path / "fork")


def test_allow_drift_forks_anyway_and_says_so(tmp_path: Path):
    harness, result = _failing_parent(tmp_path)
    (harness / "validators" / "check_summary.py").write_text("# edited after the run\n")

    forked = fork_mod.create_fork(result.trace_path, tmp_path / "fork", allow_drift=True)

    assert any("has changed since the parent run" in w for w in forked.warnings)
    assert (forked.directory / "validators" / "check_summary.py").read_text().startswith("#")


def test_a_broken_parent_chain_refuses_the_fork(tmp_path: Path):
    _, result = _failing_parent(tmp_path)
    path = Path(result.trace_path)
    lines = path.read_text().splitlines()
    event = json.loads(lines[1])
    event["payload"]["tampered"] = True
    lines[1] = json.dumps(event)
    path.write_text("\n".join(lines) + "\n")

    with pytest.raises(SpecError, match="hash chain is broken"):
        fork_mod.create_fork(result.trace_path, tmp_path / "fork")


def test_redacted_spans_in_the_prefix_are_warned_about(tmp_path: Path):
    """Redaction is pre-persistence, so a fork replays the marker, not the value."""
    harness = _harness(tmp_path)
    spec_path = harness / "harness.yaml"
    spec_path.write_text(spec_path.read_text() + "\nlogging:\n  redact:\n    - 'fox'\n")
    provider = FakeModelProvider(
        [tool_response("file_read", {"path": "notes.txt"}, call_id="c1"),
         text_response("not json")]
    )
    result = runner.run_harness(harness, "notes.txt", provider=provider, ingest=False)

    forked = fork_mod.create_fork(result.trace_path, tmp_path / "fork")

    assert any("[REDACTED]" in w for w in forked.warnings)


def test_a_pre_1_0_journal_cannot_be_forked(tmp_path: Path):
    path = tmp_path / "old.jsonl"
    path.write_text(
        json.dumps(
            {
                "run_id": "run_old",
                "seq": 0,
                "type": "run_started",
                "payload": {"input": "x"},
            }
        )
        + "\n"
        + json.dumps(
            {
                "run_id": "run_old",
                "seq": 1,
                "type": "model_call",
                "payload": {"system": "sp", "messages": [{"role": "user", "content": "x"}]},
            }
        )
        + "\n"
    )
    with pytest.raises(SpecError, match="carries no harness snapshot"):
        fork_mod.create_fork(path, tmp_path / "fork")


# --------------------------------------------------------------------------- #
# Resuming
# --------------------------------------------------------------------------- #
def test_a_resumed_fork_continues_the_parent_prefix(tmp_path: Path):
    _, result = _failing_parent(tmp_path)
    forked = fork_mod.create_fork(result.trace_path, tmp_path / "fork")
    seeded = fork_mod.load_fork_context(forked.directory)

    provider = FakeModelProvider([text_response(_VALID_SUMMARY)])
    resumed = runner.run_harness(
        forked.directory,
        resume_messages=seeded,
        lineage={"parent_run_id": result.run_id, "forked_at_seq": forked.at_seq},
        provider=provider,
        ingest=False,
    )

    assert resumed.status == "success"
    # The fork re-sent the parent's prefix verbatim and added nothing to it.
    assert provider.calls[0]["messages"] == seeded


def test_a_resumed_run_records_its_lineage(tmp_path: Path):
    _, result = _failing_parent(tmp_path)
    forked = fork_mod.create_fork(result.trace_path, tmp_path / "fork")

    resumed = runner.run_harness(
        forked.directory,
        resume_messages=fork_mod.load_fork_context(forked.directory),
        lineage={"parent_run_id": result.run_id, "forked_at_seq": forked.at_seq},
        provider=FakeModelProvider([text_response(_VALID_SUMMARY)]),
        ingest=False,
    )

    started = read_events(resumed.trace_path)[0]["payload"]
    assert started["resumed"] is True
    assert started["lineage"]["parent_run_id"] == result.run_id
    assert started["lineage"]["forked_at_seq"] == forked.at_seq


def test_resume_messages_rejects_a_second_input_form(tmp_path: Path):
    harness = _harness(tmp_path)
    with pytest.raises(ValueError, match="pass 'resume_messages' alone"):
        runner.run_harness(
            harness,
            "notes.txt",
            resume_messages=[{"role": "user", "content": "x"}],
            provider=FakeModelProvider([]),
            ingest=False,
        )


# --------------------------------------------------------------------------- #
# Lineage in the Hive
# --------------------------------------------------------------------------- #
def test_the_hive_relates_a_fork_to_its_parent(tmp_path: Path):
    _, result = _failing_parent(tmp_path)
    forked = fork_mod.create_fork(result.trace_path, tmp_path / "fork")
    resumed = runner.run_harness(
        forked.directory,
        resume_messages=fork_mod.load_fork_context(forked.directory),
        lineage={"parent_run_id": result.run_id, "forked_at_seq": forked.at_seq},
        provider=FakeModelProvider([text_response(_VALID_SUMMARY)]),
        ingest=False,
    )

    with Hive(tmp_path / "hive.db") as hive:
        hive.ingest_trace_file(result.trace_path)
        hive.ingest_trace_file(resumed.trace_path)

        tree = hive.lineage(result.run_id)
        assert [f["run_id"] for f in tree["forks"]] == [resumed.run_id]
        assert tree["forks"][0]["forked_at_seq"] == forked.at_seq

        child = hive.lineage(resumed.run_id)
        assert [a["run_id"] for a in child["ancestors"]] == [result.run_id]
        assert child["forks"] == []


def test_lineage_of_an_unforked_run_is_empty(tmp_path: Path):
    _, result = _failing_parent(tmp_path)
    with Hive(tmp_path / "hive.db") as hive:
        hive.ingest_trace_file(result.trace_path)
        tree = hive.lineage(result.run_id)
    assert tree["ancestors"] == [] and tree["forks"] == []


# --------------------------------------------------------------------------- #
# Fork x swap: the A/B (Phase 4)
# --------------------------------------------------------------------------- #
def test_fork_model_rewrites_the_spec_and_rehashes(tmp_path: Path):
    _, result = _failing_parent(tmp_path)

    control = fork_mod.create_fork(result.trace_path, tmp_path / "a")
    treatment = fork_mod.create_fork(
        result.trace_path, tmp_path / "b", model="claude-sonnet-5"
    )

    from hiveloom.spec.loader import load_spec

    assert load_spec(control.directory / "harness.yaml").model.id == "claude-haiku-4-5"
    assert load_spec(treatment.directory / "harness.yaml").model.id == "claude-sonnet-5"
    # Different harness, therefore a different fitness bucket. That is the point:
    # neither arm is held out as "swapped".
    assert treatment.version_hash != control.version_hash
    assert treatment.model_override == {
        "from": "claude:claude-haiku-4-5",
        "provider": "claude",
        "model": "claude-sonnet-5",
    }


def test_both_arms_replay_a_byte_identical_prefix(tmp_path: Path):
    _, result = _failing_parent(tmp_path)

    control = fork_mod.create_fork(result.trace_path, tmp_path / "a")
    treatment = fork_mod.create_fork(
        result.trace_path, tmp_path / "b", model="claude-sonnet-5"
    )

    assert fork_mod.load_fork_context(control.directory) == fork_mod.load_fork_context(
        treatment.directory
    )


def test_only_the_model_differs_between_the_arms(tmp_path: Path):
    _, result = _failing_parent(tmp_path)
    control = fork_mod.create_fork(result.trace_path, tmp_path / "a")
    treatment = fork_mod.create_fork(
        result.trace_path, tmp_path / "b", model="claude-sonnet-5"
    )

    from hiveloom.spec.loader import load_spec

    a = load_spec(control.directory / "harness.yaml").model_dump()
    b = load_spec(treatment.directory / "harness.yaml").model_dump()
    differing = {k for k in a if a[k] != b[k]}
    assert differing == {"model"}


def test_a_rejected_model_leaves_no_half_built_fork(tmp_path: Path):
    _, result = _failing_parent(tmp_path)
    target = tmp_path / "bad"

    with pytest.raises(Exception, match="unknown model provider"):
        fork_mod.create_fork(result.trace_path, target, model="x", model_provider="nope")

    assert not target.exists()


def test_provider_alone_re_serves_the_same_model(tmp_path: Path):
    _, result = _failing_parent(tmp_path)
    forked = fork_mod.create_fork(
        result.trace_path, tmp_path / "f", model="qwen3:4b", model_provider="ollama"
    )
    assert forked.model_override["provider"] == "ollama"


def test_the_two_arms_are_comparable_in_the_hive(tmp_path: Path):
    _, parent = _failing_parent(tmp_path)
    control = fork_mod.create_fork(parent.trace_path, tmp_path / "a")
    treatment = fork_mod.create_fork(
        parent.trace_path, tmp_path / "b", model="claude-sonnet-5"
    )

    runs = []
    for forked, script in (
        (control, [text_response("not json again")]),
        (treatment, [text_response(_VALID_SUMMARY)]),
    ):
        runs.append(
            runner.run_harness(
                forked.directory,
                resume_messages=fork_mod.load_fork_context(forked.directory),
                lineage={
                    "parent_run_id": parent.run_id,
                    "forked_at_seq": forked.at_seq,
                },
                provider=FakeModelProvider(script),
                ingest=False,
            )
        )

    assert [r.status for r in runs] == ["verify_failed", "success"]

    with Hive(tmp_path / "hive.db") as hive:
        hive.ingest_trace_file(parent.trace_path)
        for r in runs:
            hive.ingest_trace_file(r.trace_path)
        tree = hive.lineage(parent.run_id)

    assert len(tree["forks"]) == 2
    # Same divergence point, different harness versions, neither one swapped.
    assert {f["forked_at_seq"] for f in tree["forks"]} == {control.at_seq}
    assert len({f["harness_version_hash"] for f in tree["forks"]}) == 2
    assert all(">" not in (f["model_path"] or "") for f in tree["forks"])


# --------------------------------------------------------------------------- #
# Trust
# --------------------------------------------------------------------------- #
def test_a_fork_of_a_trusted_folder_inherits_trust(tmp_path: Path, monkeypatch):
    from hiveloom import trust

    harness, result = _failing_parent(tmp_path)
    monkeypatch.delenv("HIVELOOM_TRUST", raising=False)
    assert trust.is_trusted(harness)

    forked = fork_mod.create_fork(result.trace_path, tmp_path / "f")

    assert forked.trust_inherited
    assert trust.is_trusted(forked.directory)


def test_a_fork_built_from_journal_bodies_is_never_trusted(tmp_path: Path, monkeypatch):
    """A journal is a file someone can hand you; its chain proves no provenance."""
    from hiveloom import trust

    harness = _harness(tmp_path)
    spec_path = harness / "harness.yaml"
    spec_path.write_text(spec_path.read_text() + "\nlogging:\n  snapshot_files: true\n")
    result = runner.run_harness(
        harness,
        "notes.txt",
        provider=FakeModelProvider([text_response("not json")]),
        ingest=False,
    )
    monkeypatch.delenv("HIVELOOM_TRUST", raising=False)

    forked = fork_mod.create_fork(result.trace_path, tmp_path / "f")

    assert not forked.trust_inherited
    assert not trust.is_trusted(forked.directory)
    assert any("not trusted" in w for w in forked.warnings)


def test_a_fork_of_an_untrusted_folder_is_not_trusted(tmp_path: Path, monkeypatch):
    from hiveloom import trust

    harness, result = _failing_parent(tmp_path)
    monkeypatch.delenv("HIVELOOM_TRUST", raising=False)
    trust.revoke_trust(harness)

    forked = fork_mod.create_fork(result.trace_path, tmp_path / "f")

    assert not forked.trust_inherited
