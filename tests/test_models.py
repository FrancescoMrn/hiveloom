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
