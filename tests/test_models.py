"""Tests for model providers and normalized I/O types."""

from __future__ import annotations

import pytest

from hiveloom.models.fake import FakeModelProvider, text_response, tool_response
from hiveloom.models.provider import ModelConfig, Usage, estimate_tokens


def test_estimate_tokens():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("a" * 400) == 100


def test_estimated_cost_haiku():
    provider = FakeModelProvider([])
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    cost = provider.estimated_cost(usage, "claude-haiku-4-5")
    assert cost == 6.0  # $1 input + $5 output


def test_estimated_cost_prices_cache_traffic():
    provider = FakeModelProvider([])
    # Cache reads bill at 0.1x input price, writes at 1.25x (Anthropic multipliers).
    read_cost = provider.estimated_cost(
        Usage(cache_read_tokens=1_000_000), "claude-haiku-4-5"
    )
    write_cost = provider.estimated_cost(
        Usage(cache_write_tokens=1_000_000), "claude-haiku-4-5"
    )
    assert read_cost == pytest.approx(0.1)
    assert write_cost == pytest.approx(1.25)


def test_estimated_cost_unknown_model_falls_back():
    provider = FakeModelProvider([])
    cost = provider.estimated_cost(Usage(input_tokens=1_000_000, output_tokens=0), "who-knows")
    assert cost == 1.0  # falls back to haiku input price


def test_fake_provider_returns_scripted_in_order():
    provider = FakeModelProvider([tool_response("t", {"x": 1}), text_response("done")])
    cfg = ModelConfig(id="claude-haiku-4-5")
    r1 = provider.complete(system="s", messages=[], tools=[], config=cfg)
    assert r1.tool_calls[0].name == "t"
    r2 = provider.complete(system="s", messages=[], tools=[], config=cfg)
    assert r2.text == "done"
    assert len(provider.calls) == 2


def test_fake_provider_defaults_when_dry():
    provider = FakeModelProvider([])
    r = provider.complete(system="s", messages=[], tools=[], config=ModelConfig(id="x"))
    assert r.text == "done"


def test_the_builtin_catalog_covers_every_model_the_provider_handles():
    """A model `models/claude.py` special-cases must be one the spec accepts.

    The two drifted apart once already: `_NO_SAMPLING_PREFIXES` gained
    `claude-opus-5` while the pricing table did not, so the runtime could drive
    a model that `hiveloom validate` rejected.
    """
    from hiveloom import ext
    from hiveloom.models.claude import _NO_SAMPLING_PREFIXES

    ext.ensure_environment_loaded()
    known = {mid for mid, info in ext._registry.models.items() if info.provider == "claude"}

    for prefix in _NO_SAMPLING_PREFIXES:
        assert any(mid.startswith(prefix) for mid in known), (
            f"models/claude.py handles '{prefix}' but no such model is in the catalog"
        )


def test_claude_models_are_priced_and_never_free():
    """A zero price would silently disable the cost guardrail for that model."""
    from hiveloom import ext

    ext.ensure_environment_loaded()
    for mid, info in ext._registry.models.items():
        if info.provider != "claude":
            continue
        assert info.input_cost_per_mtok > 0, mid
        assert info.output_cost_per_mtok > info.input_cost_per_mtok, mid


def test_opus_5_validates_in_a_spec():
    from hiveloom.spec.schema import HarnessSpec

    spec = HarnessSpec.model_validate(
        {
            "name": "t",
            "description": "d",
            "system_prompt": "sp",
            "model": {"provider": "claude", "id": "claude-opus-5"},
        }
    )
    assert spec.model.id == "claude-opus-5"
