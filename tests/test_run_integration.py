"""End-to-end integration tests of `run` on the example harness (fake provider)."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

from hiveloom import construct, runner
from hiveloom.generate.llm import FakeStrongModel, StrongModel
from hiveloom.logging.hive import Hive
from hiveloom.models.fake import FakeModelProvider, text_response, tool_response

EXAMPLE_HARNESS = Path(__file__).resolve().parents[1] / "harnesses" / "example-summarizer"

_VALID_SUMMARY = json.dumps(
    {"title": "Fox", "summary": "A fox jumps a dog.", "key_points": ["fox", "dog"]}
)


def _make_harness(tmp_path: Path) -> Path:
    target = tmp_path / "summarizer"
    shutil.copytree(EXAMPLE_HARNESS, target)
    (target / "notes.txt").write_text("The quick brown fox jumps over the lazy dog. " * 20)
    return target


def _event_types(trace_path: str) -> list[str]:
    return [json.loads(line)["type"] for line in Path(trace_path).read_text().splitlines()]


def test_full_run_success_emits_ordered_events(tmp_path: Path):
    harness = _make_harness(tmp_path)
    provider = FakeModelProvider(
        [
            tool_response("file_read", {"path": "notes.txt"}, call_id="c1"),
            text_response(_VALID_SUMMARY),
        ]
    )
    result = runner.run_harness(harness, "notes.txt", provider=provider)

    assert result.status == "success"
    assert result.output == _VALID_SUMMARY
    assert result.cost_usd > 0

    events = _event_types(result.trace_path)
    assert events[0] == "run_started"
    assert events[-1] == "run_finished"
    assert "tool_call" in events and "tool_result" in events
    assert events.count("verification_result") == 2  # output_schema + code validator
    model_call = next(
        json.loads(line)
        for line in Path(result.trace_path).read_text().splitlines()
        if json.loads(line)["type"] == "model_call"
    )
    assert model_call["payload"]["messages"]
    assert "system" in model_call["payload"]


def test_verify_failure_triggers_retry_with_feedback(tmp_path: Path):
    harness = _make_harness(tmp_path)
    provider = FakeModelProvider(
        [
            text_response("not valid json at all"),
            text_response(_VALID_SUMMARY),
        ]
    )
    result = runner.run_harness(harness, "notes.txt", provider=provider)

    assert result.status == "success"
    # First attempt fails verification, second passes.
    verifications = [
        json.loads(line)
        for line in Path(result.trace_path).read_text().splitlines()
        if json.loads(line)["type"] == "verification_result"
    ]
    assert any(v["payload"]["passed"] is False for v in verifications)
    assert any(v["payload"]["passed"] is True for v in verifications)


def test_guardrail_halts_on_cost(tmp_path: Path):
    harness = _make_harness(tmp_path)
    # One response with huge output tokens: 100k out * $5/1M = $0.50 > the 0.25 limit.
    provider = FakeModelProvider([text_response(_VALID_SUMMARY, output_tokens=100_000)])
    result = runner.run_harness(harness, "notes.txt", provider=provider)

    assert result.status == "guardrail_halt"
    assert "cost" in result.reason
    assert "guardrail_triggered" in _event_types(result.trace_path)


def test_tool_allowlist_blocks_unregistered_tool(tmp_path: Path):
    harness = _make_harness(tmp_path)
    provider = FakeModelProvider(
        [
            tool_response("evil_tool", {}, call_id="c1"),
            text_response(_VALID_SUMMARY),
        ]
    )
    result = runner.run_harness(harness, "notes.txt", provider=provider)

    assert result.status == "success"  # blocked tool is surfaced, run recovers
    events = _event_types(result.trace_path)
    assert "guardrail_triggered" in events


def test_dry_run_uses_no_provider(tmp_path: Path):
    harness = _make_harness(tmp_path)
    info = runner.dry_run(harness, "notes.txt")
    assert info["name"] == "example-summarizer"
    assert "file_read" in [t["name"] for t in info["tools"]]
    assert info["estimated_input_tokens"] > 0


# --------------------------------------------------------------------------- #
# The post-run auto-propose trigger (opt-in `evolution.auto_propose`)
# --------------------------------------------------------------------------- #
_AUTO_PROPOSAL_PAYLOAD = json.dumps(
    {"rationale": "tighten", "yaml_changes": [{"path": "loop.max_turns", "value": 10}]}
)


def _auto_harness(
    tmp_path: Path,
    *,
    name: str = "auto-demo",
    min_failures: int = 1,
    cooldown_hours: float = 24.0,
) -> Path:
    """A freshly-constructed harness with auto_propose opted in."""
    target = tmp_path / name
    construct.init_harness(target, name=name, task="Do a small thing.")
    construct.set_value(target, "evolution.auto_propose.enabled", True)
    construct.set_value(target, "evolution.auto_propose.min_failures", min_failures)
    construct.set_value(target, "evolution.auto_propose.cooldown_hours", cooldown_hours)
    return target


def _failing_provider() -> FakeModelProvider:
    # 300k output tokens at the $5/1M fallback rate = $1.50, over the default
    # $1.00 max_cost_usd guardrail every freshly constructed harness gets.
    return FakeModelProvider([text_response("x", output_tokens=300_000)])


class _RaisingStrongModel(StrongModel):
    """Simulates a network/API failure resolving the strong model."""

    def generate(self, *, system: str, user: str, max_tokens: int = 4096) -> str:
        raise RuntimeError("boom: no network in tests")


def test_auto_propose_drafts_after_one_failing_run(tmp_path: Path):
    harness = _auto_harness(tmp_path, min_failures=1)
    model = FakeStrongModel([_AUTO_PROPOSAL_PAYLOAD])

    result = runner.run_harness(harness, "x", provider=_failing_provider(), strong_model=model)

    assert result.status == "guardrail_halt"
    with Hive() as hive:
        proposals = hive.list_proposals(harness_name="auto-demo")
    assert len(proposals) == 1
    assert proposals[0]["status"] == "pending"
    assert proposals[0]["trigger"] == "auto"


def test_auto_propose_second_failure_dedups_and_skips_the_model(tmp_path: Path):
    """The auto path benefits from the same dedup-before-model-call ordering
    as `evolve --propose`: a second failing run must not pay for a second
    strong-model call just because it re-attempts create_proposal."""
    harness = _auto_harness(tmp_path, min_failures=1, cooldown_hours=24.0)
    model = FakeStrongModel([_AUTO_PROPOSAL_PAYLOAD])  # only ONE response scripted

    runner.run_harness(harness, "x", provider=_failing_provider(), strong_model=model)
    with Hive() as hive:
        first = hive.list_proposals(harness_name="auto-demo")
        assert len(first) == 1
        # Force the cooldown window open regardless of real elapsed wall-clock
        # time between the two run_harness() calls below, so the second run
        # reaches create_proposal's dedup pre-check instead of being blocked
        # by the cooldown guard first (which would prove nothing about dedup).
        hive.update_proposal(
            first[0]["id"], created_at=(datetime.now(UTC) - timedelta(hours=48)).isoformat()
        )

    runner.run_harness(harness, "x", provider=_failing_provider(), strong_model=model)

    with Hive() as hive:
        after = hive.list_proposals(harness_name="auto-demo")
    assert len(after) == 1  # dedup held — no duplicate proposal
    assert len(model.prompts) == 1  # the strong model was never called a second time


def test_auto_propose_successful_run_creates_nothing(tmp_path: Path):
    harness = _auto_harness(tmp_path, min_failures=1)
    model = FakeStrongModel([])

    result = runner.run_harness(
        harness, "x", provider=FakeModelProvider([text_response("done")]), strong_model=model
    )

    assert result.status == "success"
    with Hive() as hive:
        assert hive.list_proposals(harness_name="auto-demo") == []
    assert model.prompts == []


def test_auto_propose_default_disabled_makes_no_hive_proposals_query(
    tmp_path: Path, monkeypatch
):
    target = tmp_path / "plain"
    construct.init_harness(target, name="plain-demo", task="Do a small thing.")
    # evolution.auto_propose.enabled defaults to False — left untouched on purpose.

    calls: list[str] = []
    original = Hive.failure_count

    def spy(self, *args, **kwargs):
        calls.append("failure_count")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Hive, "failure_count", spy)

    result = runner.run_harness(
        target, "x", provider=_failing_provider(), strong_model=FakeStrongModel([])
    )

    assert result.status == "guardrail_halt"
    assert calls == []  # disabled short-circuits before any Hive proposals query
    with Hive() as hive:
        assert hive.list_proposals(harness_name="plain-demo") == []


def test_auto_propose_raising_strong_model_does_not_fail_the_run(tmp_path: Path):
    harness = _auto_harness(tmp_path, min_failures=1)

    result = runner.run_harness(
        harness, "x", provider=_failing_provider(), strong_model=_RaisingStrongModel()
    )

    assert result.status == "guardrail_halt"  # unaffected by the auto-propose crash
    with Hive() as hive:
        assert hive.list_proposals(harness_name="auto-demo") == []


def test_auto_propose_malformed_model_output_does_not_fail_the_run(tmp_path: Path):
    """A raising create_proposal (here: evolver.propose chokes on bad JSON) must
    not fail the run either — same never-fail discipline as trace-ingest."""
    harness = _auto_harness(tmp_path, min_failures=1)
    model = FakeStrongModel(["not valid json at all"])

    result = runner.run_harness(harness, "x", provider=_failing_provider(), strong_model=model)

    assert result.status == "guardrail_halt"
    with Hive() as hive:
        assert hive.list_proposals(harness_name="auto-demo") == []
