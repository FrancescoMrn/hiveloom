"""Deterministic, bounded incident packets for evolution analysis."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from hiveloom.logging.hive import Hive
from hiveloom.logging.journal import verify_chain
from hiveloom.logging.trace import TraceRedactor
from hiveloom.spec.schema import RedactionConfig, TraceExcerptConfig


class IncidentEvent(BaseModel):
    """One redacted event near the indexed incident."""

    seq: int
    type: str
    timestamp: str | None = None
    payload: Any = Field(default_factory=dict)
    payload_sha256: str
    payload_bytes: int
    truncated: bool = False


class IncidentPacket(BaseModel):
    """A small journal window plus enough provenance to audit its selection."""

    incident_id: str
    run_id: str
    friction_id: int | None = None
    category: str
    component: str | None = None
    recovered: bool = False
    incident_seq: int | None = None
    timestamp: str | None = None
    summary: str = ""
    source: str
    events: list[IncidentEvent] = Field(default_factory=list)
    omitted_event_count: int = 0
    journal_sha256: str = ""
    fallback_reason: str | None = None


class IncidentEvidence(BaseModel):
    """All packets admitted under one deterministic byte/token budget."""

    selection_rules: dict[str, Any]
    packets: list[IncidentPacket] = Field(default_factory=list)
    dropped_incidents: int = 0
    serialized_bytes: int = 0
    estimated_tokens: int = 0
    digest: str = ""

    def receipt(self) -> dict[str, Any]:
        """Proposal metadata without copying event payloads a second time."""
        return {
            "selection_rules": self.selection_rules,
            "run_ids": [packet.run_id for packet in self.packets],
            "friction_ids": [
                packet.friction_id
                for packet in self.packets
                if packet.friction_id is not None
            ],
            "incident_ids": [packet.incident_id for packet in self.packets],
            "packet_sources": [packet.source for packet in self.packets],
            "serialized_bytes": self.serialized_bytes,
            "estimated_tokens": self.estimated_tokens,
            "dropped_incidents": self.dropped_incidents,
            "digest": self.digest,
        }


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def _bounded_payload(payload: Any, max_bytes: int) -> tuple[Any, str, int, bool]:
    encoded = _canonical(payload)
    digest = hashlib.sha256(encoded).hexdigest()
    if len(encoded) <= max_bytes:
        return payload, digest, len(encoded), False
    preview = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return (
        {
            "_truncated": True,
            "_preview": preview,
            "_sha256": digest,
            "_original_bytes": len(encoded),
        },
        digest,
        len(encoded),
        True,
    )


def _event_window(
    trace_path: Path,
    *,
    run_id: str,
    incident_seq: int | None,
    config: TraceExcerptConfig,
    redactor: TraceRedactor,
) -> tuple[list[IncidentEvent], int, str, str | None]:
    chain = verify_chain(trace_path)
    if not chain.ok:
        return [], 0, "", f"invalid journal chain: {chain.reason}"

    sanitized: list[dict[str, Any]] = []
    digest = hashlib.sha256()
    try:
        with trace_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                event = json.loads(line)
                if not isinstance(event, dict) or event.get("run_id") != run_id:
                    return [], 0, "", "journal run identity does not match the Hive"
                clean = {
                    "seq": int(event.get("seq", 0)),
                    "type": str(event.get("type") or ""),
                    "timestamp": event.get("timestamp"),
                    "payload": redactor.redact(event.get("payload") or {}),
                }
                digest.update(_canonical(clean))
                digest.update(b"\n")
                sanitized.append(clean)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return [], 0, "", f"journal could not be read: {type(exc).__name__}"

    if not sanitized:
        return [], 0, digest.hexdigest(), "journal is empty"
    target = incident_seq if incident_seq is not None else sanitized[-1]["seq"]
    lower = target - config.before_events
    upper = target + config.after_events
    selected = [event for event in sanitized if lower <= event["seq"] <= upper]
    events: list[IncidentEvent] = []
    for event in selected:
        payload, payload_digest, payload_bytes, truncated = _bounded_payload(
            event["payload"], config.max_event_bytes
        )
        events.append(
            IncidentEvent(
                seq=event["seq"],
                type=event["type"],
                timestamp=event["timestamp"],
                payload=payload,
                payload_sha256=payload_digest,
                payload_bytes=payload_bytes,
                truncated=truncated,
            )
        )
    return events, len(sanitized) - len(selected), digest.hexdigest(), None


def _packet(
    hive: Hive,
    seed: dict[str, Any],
    *,
    config: TraceExcerptConfig,
    redactor: TraceRedactor,
) -> IncidentPacket:
    run = hive.get_run(seed["run_id"])
    fallback: str | None = None
    events: list[IncidentEvent] = []
    omitted = 0
    journal_digest = ""
    if run is None:
        fallback = "run is not indexed"
    elif not run.get("trace_path"):
        fallback = (
            f"raw journal was pruned at {run['trace_pruned_at']}"
            if run.get("trace_pruned_at")
            else "raw journal path is unavailable"
        )
    else:
        trace_path = Path(run["trace_path"])
        if not trace_path.is_file():
            fallback = "raw journal file is missing"
        else:
            events, omitted, journal_digest, fallback = _event_window(
                trace_path,
                run_id=seed["run_id"],
                incident_seq=seed.get("incident_seq"),
                config=config,
                redactor=redactor,
            )
    return IncidentPacket(
        incident_id=seed["incident_id"],
        run_id=seed["run_id"],
        friction_id=seed.get("friction_id"),
        category=seed["category"],
        component=seed.get("component"),
        recovered=bool(seed.get("recovered")),
        incident_seq=seed.get("incident_seq"),
        timestamp=seed.get("timestamp"),
        summary=str(redactor.redact(seed.get("summary") or "")),
        source=seed["source"],
        events=events,
        omitted_event_count=omitted,
        journal_sha256=journal_digest,
        fallback_reason=fallback,
    )


def _seeds(
    recent_friction: list[dict[str, Any]],
    outcome_failures: list[dict[str, Any]],
    recent_failures: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    seeds: dict[str, dict[str, Any]] = {}
    for event in recent_friction:
        incident_id = f"friction:{event['friction_id']}"
        seeds[incident_id] = {
            "incident_id": incident_id,
            "run_id": event["run_id"],
            "friction_id": event["friction_id"],
            "category": event["category"],
            "component": event.get("component"),
            "recovered": event["recovered"],
            "incident_seq": event["seq"],
            "timestamp": event.get("timestamp"),
            "summary": event.get("summary", ""),
            "source": "friction",
        }
    for outcome in outcome_failures:
        incident_id = f"outcome:{outcome['run_id']}"
        seeds.setdefault(
            incident_id,
            {
                "incident_id": incident_id,
                "run_id": outcome["run_id"],
                "category": "external_outcome",
                "timestamp": outcome.get("recorded_at"),
                "summary": outcome.get("detail", "") or "external outcome was failure",
                "source": "run_outcome",
            },
        )
    seeded_runs = {seed["run_id"] for seed in seeds.values()}
    for failure in recent_failures:
        if failure["run_id"] in seeded_runs:
            continue
        incident_id = f"failure:{failure['run_id']}"
        seeds[incident_id] = {
            "incident_id": incident_id,
            "run_id": failure["run_id"],
            "category": "final_failure",
            "timestamp": failure.get("finished_at"),
            "summary": failure.get("reason", "") or failure.get("status", "failure"),
            "source": "run_status",
        }
    return sorted(
        seeds.values(),
        key=lambda seed: (str(seed.get("timestamp") or ""), seed["incident_id"]),
        reverse=True,
    )


def build_incident_evidence(
    hive: Hive,
    *,
    recent_friction: list[dict[str, Any]],
    outcome_failures: list[dict[str, Any]],
    recent_failures: list[dict[str, Any]],
    config: TraceExcerptConfig,
    redaction: RedactionConfig,
) -> IncidentEvidence:
    """Select newest incidents and enforce the serialized packet budget."""
    rules = config.model_dump(mode="json")
    redactor = TraceRedactor(
        patterns=redaction.patterns,
        keys=redaction.keys,
        paths=redaction.paths,
    )
    candidates = _seeds(recent_friction, outcome_failures, recent_failures)
    outside_incident_limit = max(0, len(candidates) - config.max_incidents)
    candidates = candidates[: config.max_incidents]
    effective_bytes = min(config.max_bytes, config.max_tokens * 4)
    packets: list[IncidentPacket] = []
    dropped = 0

    for seed in candidates:
        packet = _packet(hive, seed, config=config, redactor=redactor)
        trial = [*packets, packet]
        encoded = _canonical([item.model_dump(mode="json") for item in trial])
        if len(encoded) > effective_bytes:
            packet = packet.model_copy(
                update={
                    "events": [],
                    "summary": packet.summary[:128],
                    "fallback_reason": packet.fallback_reason or "event window omitted by budget",
                }
            )
            trial = [*packets, packet]
            encoded = _canonical([item.model_dump(mode="json") for item in trial])
        if len(encoded) > effective_bytes:
            dropped += 1
            continue
        packets.append(packet)

    encoded = _canonical([packet.model_dump(mode="json") for packet in packets])
    return IncidentEvidence(
        selection_rules=rules,
        packets=packets,
        dropped_incidents=dropped + outside_incident_limit,
        serialized_bytes=len(encoded),
        estimated_tokens=math.ceil(len(encoded) / 4),
        digest=hashlib.sha256(encoded).hexdigest(),
    )
