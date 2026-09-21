"""Provider request controls and runtime propagation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hiveloom import construct, runner
from hiveloom.models.fake import FakeModelProvider, text_response
from hiveloom.models.provider import ModelConfig
from hiveloom.spec.schema import ModelConfig as SpecModelConfig


class RecordingProvider(FakeModelProvider):
    def __init__(self, responses):
        super().__init__(responses)
        self.configs = []

    def complete(self, **kwargs):
        self.configs.append(kwargs["config"].model_copy(deep=True))
        return super().complete(**kwargs)


def test_model_params_reach_the_provider_payload(monkeypatch):
    from hiveloom.models.openai_compat import OpenAICompatProvider

    sent: dict = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(
                {"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]}
            ).encode()

    def fake_urlopen(request, timeout=0):
        sent.update(json.loads(request.data.decode()))
        return _Response()

    monkeypatch.setattr("hiveloom.models.openai_compat.urlrequest.urlopen", fake_urlopen)
    OpenAICompatProvider("http://localhost:9").complete(
        system="s",
        messages=[{"role": "user", "content": "x"}],
        tools=[],
        config=ModelConfig(id="m", params={"reasoning": {"enabled": False}}),
    )

    assert sent["reasoning"] == {"enabled": False}
    assert sent["model"] == "m"


def test_model_params_cannot_restate_what_the_harness_owns():
    for field in ("model", "messages", "tools", "max_tokens", "temperature"):
        with pytest.raises(ValueError, match="model.params cannot set"):
            SpecModelConfig(provider="openai", id="gpt-4o", params={field: "x"})


def test_model_params_default_to_nothing_sent(monkeypatch):
    """Every provider that needs no extra fields must see the payload unchanged."""
    from hiveloom.models.openai_compat import OpenAICompatProvider

    sent: dict = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(
                {"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]}
            ).encode()

    monkeypatch.setattr(
        "hiveloom.models.openai_compat.urlrequest.urlopen",
        lambda request, timeout=0: (sent.update(json.loads(request.data.decode())), _Response())[1],
    )
    OpenAICompatProvider("http://localhost:9").complete(
        system="s",
        messages=[{"role": "user", "content": "x"}],
        tools=[],
        config=ModelConfig(id="m"),
    )

    assert set(sent) == {"model", "max_tokens", "messages"}


def test_a_model_override_keeps_the_declared_provider_params(tmp_path: Path, monkeypatch):
    """An override selects which model runs, not how the harness calls it.

    Every eval cell goes through this path, so dropping `params` here sent a
    pinned run to whichever upstream a router chose — indistinguishable from
    model variance in the results, and invisible in the spec on disk.
    """
    from hiveloom.runner import _apply_runtime_model_overrides
    from hiveloom.spec.schema import HarnessSpec

    spec = HarnessSpec.model_validate(
        {
            "name": "h",
            "description": "d",
            "system_prompt": "s",
            "model": {
                "provider": "openai",
                "id": "gpt-4o",
                "params": {"provider": {"order": ["acme/fp8"], "allow_fallbacks": False}},
            },
        }
    )

    overridden = _apply_runtime_model_overrides(spec, "gpt-4.1", None)

    assert overridden.model.id == "gpt-4.1"
    assert overridden.model.params == {
        "provider": {"order": ["acme/fp8"], "allow_fallbacks": False}
    }


def test_run_passes_provider_params_to_the_real_router(harness_dir: Path):
    construct.set_value(harness_dir, "model.params", {"seed": 7})
    provider = RecordingProvider([text_response("ok")])
    runner.run_harness(harness_dir, "go", provider=provider)
    assert provider.configs[0].params == {"seed": 7}


@pytest.mark.parametrize("field", ["max_completion_tokens", "max_output_tokens", "system",
                                   "input", "functions", "stream", "stream_options", "n"])
def test_provider_aliases_cannot_bypass_harness_request_controls(field):
    for cls in (SpecModelConfig, ModelConfig):
        with pytest.raises(ValueError, match="model.params cannot set"):
            cls(id="claude-haiku-4-5", params={field: 1})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), object()])
def test_provider_params_must_be_json_safe(value):
    with pytest.raises(ValueError, match="JSON-safe"):
        SpecModelConfig(params={"seed": value})
