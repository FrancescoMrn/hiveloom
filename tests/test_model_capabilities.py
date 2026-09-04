"""Provider-neutral capability probes and effective-model enforcement."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

from hiveloom import construct, ext
from hiveloom.cli import app
from hiveloom.errors import ExitCode, SpecError
from hiveloom.models.capabilities import probe_model, require_compatible_probe
from hiveloom.models.fake import FakeModelProvider, text_response
from hiveloom.models.provider import ModelResponse, ToolCall, Usage

cli = CliRunner()


def _declare_model(model_id: str = "requested-model") -> None:
    ext.ensure_environment_loaded()
    ext.ExtensionAPI(source="test:probe").register_model(
        ext.ModelInfo(
            id=model_id,
            provider="fixture",
            supports_tool_calling=True,
            supports_structured_output=True,
            supports_reasoning_replay=True,
        )
    )


def _tool_probe_response(model: str, *, reasoning: bool = True) -> ModelResponse:
    return ModelResponse(
        tool_calls=[
            ToolCall(id="probe-call", name="hiveloom_capability_probe", input={"token": "ok"})
        ],
        stop_reason="tool_use",
        usage=Usage(input_tokens=10, output_tokens=2),
        content_blocks=[
            {
                "type": "tool_use",
                "id": "probe-call",
                "name": "hiveloom_capability_probe",
                "input": {"token": "ok"},
            }
        ],
        model=model,
        provider_request_id="request-1",
        reasoning={"details": "opaque"} if reasoning else None,
    )


def test_live_probe_observes_tools_replays_reasoning_and_keeps_declarations():
    _declare_model()
    second = text_response("done")
    second.model = "requested-model"
    second.provider_request_id = "request-2"
    provider = FakeModelProvider([_tool_probe_response("requested-model"), second])

    result = probe_model(
        "fixture",
        "requested-model",
        provider=provider,
        live=True,
        policy="exact",
    )

    assert result.identity.status == "exact"
    assert result.identity.accepted is True
    assert result.capabilities["tool_calling"].model_dump() == {
        "value": True,
        "source": "observed",
    }
    assert result.capabilities["reasoning_replay"].source == "observed"
    assert result.capabilities["structured_output"].source == "declared"
    assert result.calls == 2
    assert result.provider_request_ids == ["request-1", "request-2"]
    assert len(provider.calls) == 2
    replay = provider.calls[1]["messages"]
    assert any(
        block.get("type") == "provider_reasoning"
        for block in replay[1]["content"]
    )


def test_wrong_model_is_rejected_or_explicitly_accepted_as_alias():
    _declare_model()
    wrong = FakeModelProvider([_tool_probe_response("served-model", reasoning=False)])
    rejected = probe_model(
        "fixture",
        "requested-model",
        provider=wrong,
        live=True,
        policy="exact",
        refresh=True,
    )

    assert rejected.identity.status == "mismatch"
    assert rejected.identity.accepted is False
    with pytest.raises(SpecError, match="served-model"):
        require_compatible_probe(rejected)

    alias_provider = FakeModelProvider(
        [_tool_probe_response("served-model", reasoning=False)]
    )
    accepted = probe_model(
        "fixture",
        "requested-model",
        provider=alias_provider,
        live=True,
        policy="alias",
        aliases=["served-model"],
        refresh=True,
    )
    assert accepted.identity.status == "alias"
    assert accepted.identity.accepted is True
    assert accepted.effective_models == ["served-model"]


def test_declared_only_probe_has_no_identity_evidence_or_provider_io():
    _declare_model()
    result = probe_model("fixture", "requested-model", live=False, policy="exact")

    assert result.live is False
    assert result.calls == 0
    assert result.identity.status == "unknown"
    assert result.identity.accepted is False
    assert result.capabilities["tool_calling"].source == "declared"


def test_live_probe_reports_observed_missing_tool_support():
    _declare_model()
    response = text_response("I cannot call that tool")
    response.model = "requested-model"
    provider = FakeModelProvider([response])

    result = probe_model(
        "fixture",
        "requested-model",
        provider=provider,
        live=True,
        policy="exact",
        refresh=True,
    )

    assert result.capabilities["tool_calling"].model_dump() == {
        "value": False,
        "source": "observed",
    }
    assert result.capabilities["structured_output"].source == "declared"
    assert result.capabilities["reasoning_replay"].source == "declared"


def test_live_probe_cache_expires_and_adapter_change_invalidates():
    _declare_model()
    start = datetime(2026, 1, 1, tzinfo=UTC)
    first_provider = FakeModelProvider(
        [_tool_probe_response("requested-model", reasoning=False)]
    )
    first = probe_model(
        "fixture",
        "requested-model",
        provider=first_provider,
        live=True,
        policy="exact",
        ttl_seconds=60,
        now=start,
    )
    cached_provider = FakeModelProvider([])
    cached = probe_model(
        "fixture",
        "requested-model",
        provider=cached_provider,
        live=True,
        policy="exact",
        now=start + timedelta(seconds=30),
    )

    class ChangedAdapter(FakeModelProvider):
        pass

    changed_provider = ChangedAdapter(
        [_tool_probe_response("requested-model", reasoning=False)]
    )
    changed = probe_model(
        "fixture",
        "requested-model",
        provider=changed_provider,
        live=True,
        policy="exact",
        now=start + timedelta(seconds=30),
    )
    expired_provider = FakeModelProvider(
        [_tool_probe_response("requested-model", reasoning=False)]
    )
    expired = probe_model(
        "fixture",
        "requested-model",
        provider=expired_provider,
        live=True,
        policy="exact",
        now=start + timedelta(seconds=61),
    )
    _declare_model("other-model")
    other_provider = FakeModelProvider(
        [_tool_probe_response("other-model", reasoning=False)]
    )
    other = probe_model(
        "fixture",
        "other-model",
        provider=other_provider,
        live=True,
        policy="exact",
        now=start + timedelta(seconds=30),
    )

    assert first.cached is False
    assert cached.cached is True
    assert cached_provider.calls == []
    assert changed.cached is False
    assert changed.adapter_digest != first.adapter_digest
    assert len(changed_provider.calls) == 1
    assert expired.cached is False
    assert len(expired_provider.calls) == 1
    assert other.cached is False
    assert len(other_provider.calls) == 1


def test_models_probe_cli_declared_mode_is_free_and_strict_live_rejects(
    tmp_path,
):
    created: list[FakeModelProvider] = []

    def factory(_ctx):
        provider = FakeModelProvider(
            [_tool_probe_response("wrong-model", reasoning=False)]
        )
        created.append(provider)
        return provider

    api = ext.ExtensionAPI(source="test:probe-cli")
    api.register_provider("fixture", factory, open_catalog=True, label="Fixture")
    harness = tmp_path / "h"
    construct.init_harness(harness, name="probe-harness", task="Synthetic task.")

    declared = cli.invoke(
        app,
        [
            "models",
            "probe",
            str(harness),
            "--provider",
            "fixture",
            "--model",
            "requested-model",
            "--identity",
            "exact",
            "--json",
        ],
    )
    assert declared.exit_code == ExitCode.OK
    declared_payload = json.loads(declared.stdout)
    assert declared_payload["plan"]["contacts_provider"] is False
    assert created == []

    strict = cli.invoke(
        app,
        [
            "models",
            "probe",
            str(harness),
            "--provider",
            "fixture",
            "--model",
            "requested-model",
            "--identity",
            "exact",
            "--live",
            "--refresh",
            "--require-compatible",
            "--json",
        ],
    )

    assert strict.exit_code == ExitCode.SPEC_ERROR
    assert "wrong-model" in json.loads(strict.stdout)["error"]
    assert len(created) == 1
    assert len(created[0].calls) == 1
