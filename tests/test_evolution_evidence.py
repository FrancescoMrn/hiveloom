"""Bounded incident evidence supplied to the evolution proposing model."""

from __future__ import annotations

import json
from pathlib import Path

from hiveloom import construct
from hiveloom.evolve.analyzer import analyze
from hiveloom.evolve.evolver import build_evolve_prompt
from hiveloom.evolve.proposals import create_proposal, proposal_payload
from hiveloom.generate.llm import FakeStrongModel
from hiveloom.logging.hive import Hive
from hiveloom.logging.trace import TraceWriter
from hiveloom.spec.loader import load_spec

_PROPOSAL = json.dumps(
    {
        "rationale": "Make the output contract explicit.",
        "yaml_changes": [
            {
                "path": "system_prompt",
                "value": "Return the required structure.",
                "rationale": "Recovered schema failures recur.",
            }
        ],
        "code_changes": [],
    }
)


def _harness(tmp_path: Path) -> Path:
    harness = tmp_path / "harness"
    construct.init_harness(harness, name="demo", task="Return a structured result.")
    construct.set_value(
        harness,
        "logging.redact",
        {"keys": ["email"], "patterns": ["secret-[a-z]+"]},
    )
    construct.set_value(
        harness,
        "evolution.trace_excerpts",
        {
            "enabled": True,
            "max_incidents": 5,
            "before_events": 1,
            "after_events": 1,
            "max_event_bytes": 128,
            "max_bytes": 4096,
            "max_tokens": 1024,
        },
    )
    return harness


def _recovered_trace(root: Path, spec, run_id: str = "run_recovered") -> Path:
    # Deliberately written without redaction to represent a trace produced by
    # an older policy. The current policy must still protect the model prompt.
    writer = TraceWriter(
        root,
        run_id,
        spec.name,
        "v1",
        harness_id=spec.identity,
    )
    writer.emit("run_started", input="case secret-alpha", email="person@example.test")
    writer.emit("model_call", turn=0, phase="act")
    writer.emit(
        "model_response",
        turn=1,
        phase="act",
        text="secret-alpha " + "large-output " * 100,
    )
    writer.emit(
        "verification_result",
        verifier="output_schema",
        passed=False,
        feedback="secret-alpha output did not match",
    )
    writer.emit("model_call", turn=1, phase="act")
    writer.emit("model_response", turn=2, phase="act", text="valid")
    writer.emit(
        "verification_result",
        verifier="output_schema",
        passed=True,
        feedback="",
    )
    writer.emit(
        "run_finished",
        status="success",
        turns=2,
        cost_usd=0.01,
        duration_seconds=1.0,
    )
    return writer.path


def test_evidence_is_bounded_redacted_deterministic_and_prompt_safe(tmp_path: Path):
    harness = _harness(tmp_path)
    spec = load_spec(harness)
    trace = _recovered_trace(tmp_path / "traces", spec)

    with Hive(tmp_path / "hive.db") as hive:
        hive.ingest_trace_file(trace)
        first = analyze(
            hive,
            spec.identity,
            excerpt_config=spec.evolution.trace_excerpts,
            redaction=spec.logging.redact,
        )
        second = analyze(
            hive,
            spec.identity,
            excerpt_config=spec.evolution.trace_excerpts,
            redaction=spec.logging.redact,
        )

    evidence = first.incident_evidence
    assert evidence is not None
    assert evidence.digest == second.incident_evidence.digest
    assert evidence.serialized_bytes <= spec.evolution.trace_excerpts.max_bytes
    assert evidence.estimated_tokens <= spec.evolution.trace_excerpts.max_tokens
    [packet] = evidence.packets
    assert packet.category == "output_validation"
    assert packet.recovered is True
    assert packet.source == "friction"
    assert packet.omitted_event_count > 0
    assert packet.journal_sha256
    assert any(event.truncated and event.payload_sha256 for event in packet.events)

    _, prompt = build_evolve_prompt(spec, first)
    assert "secret-alpha" not in prompt
    assert "person@example.test" not in prompt
    assert "[REDACTED]" in prompt


def test_pruned_journal_degrades_to_indexed_summary(tmp_path: Path):
    harness = _harness(tmp_path)
    spec = load_spec(harness)
    trace = _recovered_trace(tmp_path / "traces", spec)
    with Hive(tmp_path / "hive.db") as hive:
        hive.ingest_trace_file(trace)
        hive.mark_traces_pruned(
            [("run_recovered", str(trace))], pruned_at="2026-08-29T12:00:00+00:00"
        )
        report = analyze(
            hive,
            spec.identity,
            excerpt_config=spec.evolution.trace_excerpts,
            redaction=spec.logging.redact,
        )

    [packet] = report.incident_evidence.packets
    assert packet.events == []
    assert packet.summary == "[REDACTED] output did not match"
    assert "pruned at" in packet.fallback_reason


def test_missing_journal_degrades_to_indexed_summary(tmp_path: Path):
    harness = _harness(tmp_path)
    spec = load_spec(harness)
    trace = _recovered_trace(tmp_path / "traces", spec)
    with Hive(tmp_path / "hive.db") as hive:
        hive.ingest_trace_file(trace)
        trace.unlink()
        report = analyze(
            hive,
            spec.identity,
            excerpt_config=spec.evolution.trace_excerpts,
            redaction=spec.logging.redact,
        )

    [packet] = report.incident_evidence.packets
    assert packet.events == []
    assert packet.summary == "[REDACTED] output did not match"
    assert packet.fallback_reason == "raw journal file is missing"


def test_failed_external_outcome_selects_the_run_even_without_friction(tmp_path: Path):
    harness = _harness(tmp_path)
    spec = load_spec(harness)
    writer = TraceWriter(
        tmp_path / "traces", "run_outcome", spec.name, "v1", harness_id=spec.identity
    )
    writer.emit("run_started", input="case")
    writer.emit("model_response", turn=1, text="plausible but wrong")
    writer.emit("run_finished", status="success", turns=1, cost_usd=0.0)

    with Hive(tmp_path / "hive.db") as hive:
        hive.ingest_trace_file(writer.path)
        hive.record_outcome(
            "run_outcome", "failure", source="operator", detail="dismissed by reviewer"
        )
        report = analyze(
            hive,
            spec.identity,
            excerpt_config=spec.evolution.trace_excerpts,
            redaction=spec.logging.redact,
        )

    [packet] = report.incident_evidence.packets
    assert packet.category == "external_outcome"
    assert packet.source == "run_outcome"
    assert packet.events[-1].type == "run_finished"


def test_hard_budget_drops_older_incidents_and_labels_the_receipt(tmp_path: Path):
    harness = _harness(tmp_path)
    spec = load_spec(harness)
    config = spec.evolution.trace_excerpts.model_copy(
        update={"max_incidents": 1, "max_bytes": 1024, "max_tokens": 256}
    )
    first = _recovered_trace(tmp_path / "traces", spec, "run_a")
    second = _recovered_trace(tmp_path / "traces", spec, "run_b")
    with Hive(tmp_path / "hive.db") as hive:
        hive.ingest_trace_file(first)
        hive.ingest_trace_file(second)
        report = analyze(
            hive,
            spec.identity,
            excerpt_config=config,
            redaction=spec.logging.redact,
        )

    evidence = report.incident_evidence
    assert len(evidence.packets) <= 1
    assert evidence.dropped_incidents >= 1
    assert evidence.serialized_bytes <= 1024
    assert evidence.estimated_tokens <= 256
    assert evidence.receipt()["digest"] == evidence.digest


def test_proposal_stores_evidence_receipt_without_event_payloads(tmp_path: Path):
    harness = _harness(tmp_path)
    spec = load_spec(harness)
    trace = _recovered_trace(tmp_path / "traces", spec)
    with Hive(tmp_path / "hive.db") as hive:
        hive.ingest_trace_file(trace)
        report = analyze(
            hive,
            spec.identity,
            excerpt_config=spec.evolution.trace_excerpts,
            redaction=spec.logging.redact,
        )
        record = create_proposal(
            hive,
            spec,
            harness,
            report,
            FakeStrongModel([_PROPOSAL]),
            trigger="manual",
        )

    payload = proposal_payload(record)
    assert payload["evidence"]["digest"] == report.incident_evidence.digest
    assert payload["evidence"]["friction_ids"]
    assert "packets" not in record.evidence
    assert "large-output" not in record.evidence_json
