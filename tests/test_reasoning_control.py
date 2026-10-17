"""`model.reasoning`: one setting, mapped to whatever each host actually reads.

Open reasoning models spend the whole output budget thinking unless told not
to, and the field that says so differs per host: some silently ignore the wrong
one. These tests pin the per-host mapping, the escape hatch (`model.params`
wins), the records that make a reasoning-eaten run diagnosable, and the static
warning for the configuration that causes it.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from hiveloom import construct, ext, runner
from hiveloom.cli import app
from hiveloom.models.fake import FakeModelProvider, text_response
from hiveloom.models.openai_compat import OpenAICompatProvider, normalize_openai_response
from hiveloom.models.provider import ModelConfig, ModelResponse, Usage
from hiveloom.models.reasoning import (
    ReasoningUnsupportedError,
    claude_reasoning_fields,
    openai_compat_reasoning_fields,
)
from hiveloom.spec.loader import spec_warnings, validate_harness
from hiveloom.spec.schema import ALWAYS_FROZEN
from hiveloom.spec.schema import ModelConfig as SpecModelConfig


# --------------------------------------------------------------------------- #
# The per-style mapping
# --------------------------------------------------------------------------- #
def test_default_sends_nothing_for_every_style():
    for style in ("openai", "openrouter", "chat_template", "none"):
        assert openai_compat_reasoning_fields(style, "default") == {}


@pytest.mark.parametrize(
    ("style", "level", "expected"),
    [
        ("openai", "low", {"reasoning_effort": "low"}),
        ("openai", "none", {"reasoning_effort": "none"}),
        ("openrouter", "minimal", {"reasoning": {"effort": "minimal"}}),
        ("openrouter", "high", {"reasoning": {"effort": "high"}}),
        ("openrouter", "none", {"reasoning": {"enabled": False}}),
        ("chat_template", "medium", {"reasoning_effort": "medium"}),
        (
            "chat_template",
            "none",
            {"reasoning_effort": "none", "chat_template_kwargs": {"enable_thinking": False}},
        ),
    ],
)
def test_style_mapping(style, level, expected):
    assert openai_compat_reasoning_fields(style, level) == expected


def test_a_host_with_no_control_refuses_instead_of_dropping_the_setting():
    with pytest.raises(ReasoningUnsupportedError, match="no reasoning control"):
        openai_compat_reasoning_fields("none", "low")


# --------------------------------------------------------------------------- #
# What the OpenAI-compatible provider puts on the wire
# --------------------------------------------------------------------------- #
class _Response:
    def __init__(self, body: dict[str, Any]):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self._body).encode()


def _complete(monkeypatch, *, style="openai", body=None, **config) -> tuple[dict, ModelResponse]:
    sent: dict[str, Any] = {}
    reply = body or {"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]}

    def fake_urlopen(request, timeout=0):
        sent.update(json.loads(request.data.decode()))
        return _Response(reply)

    monkeypatch.setattr("hiveloom.models.openai_compat.urlrequest.urlopen", fake_urlopen)
    response = OpenAICompatProvider("http://localhost:9", reasoning_style=style).complete(
        system="s",
        messages=[{"role": "user", "content": "x"}],
        tools=[],
        config=ModelConfig(id="m", **config),
    )
    return sent, response


def test_payload_carries_the_styles_field(monkeypatch):
    sent, response = _complete(monkeypatch, style="openrouter", reasoning="none")
    assert sent["reasoning"] == {"enabled": False}
    assert "reasoning_effort" not in sent
    assert response.reasoning_sent == {"reasoning": {"enabled": False}}


def test_chat_template_off_sends_both_fields(monkeypatch):
    sent, response = _complete(monkeypatch, style="chat_template", reasoning="none")
    assert sent["reasoning_effort"] == "none"
    assert sent["chat_template_kwargs"] == {"enable_thinking": False}
    assert response.reasoning_sent == {
        "reasoning_effort": "none",
        "chat_template_kwargs": {"enable_thinking": False},
    }


def test_an_explicit_object_param_keeps_the_mapped_switch(monkeypatch):
    sent, _ = _complete(
        monkeypatch,
        style="chat_template",
        reasoning="none",
        params={"chat_template_kwargs": {"custom": 1}},
    )
    assert sent["chat_template_kwargs"] == {"enable_thinking": False, "custom": 1}
    sent, _ = _complete(
        monkeypatch,
        style="chat_template",
        reasoning="none",
        params={"chat_template_kwargs": {"enable_thinking": True}},
    )
    assert sent["chat_template_kwargs"] == {"enable_thinking": True}


def test_default_reasoning_leaves_the_payload_untouched(monkeypatch):
    sent, response = _complete(monkeypatch, style="openrouter")
    assert not {"reasoning", "reasoning_effort", "chat_template_kwargs"} & set(sent)
    assert response.reasoning_sent == {}


def test_an_explicit_param_beats_the_mapping(monkeypatch):
    sent, response = _complete(
        monkeypatch,
        style="openrouter",
        reasoning="low",
        params={"reasoning": {"effort": "high", "exclude": True}},
    )
    assert sent["reasoning"] == {"effort": "high", "exclude": True}
    # The record says what went out, not what the mapping would have sent.
    assert response.reasoning_sent == {"reasoning": {"effort": "high", "exclude": True}}


def test_a_host_with_no_control_fails_the_call_before_sending(monkeypatch):
    with pytest.raises(ReasoningUnsupportedError):
        _complete(monkeypatch, style="none", reasoning="low")


def test_the_params_escape_hatch_still_works_on_a_host_with_no_control(monkeypatch):
    sent, response = _complete(
        monkeypatch, style="none", params={"thinking": {"type": "disabled"}}
    )
    assert sent["thinking"] == {"type": "disabled"}
    assert response.reasoning_sent == {}


def test_reasoning_tokens_are_read_from_usage_details():
    response = normalize_openai_response(
        {
            "choices": [{"finish_reason": "length", "message": {"content": ""}}],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 1200,
                "completion_tokens_details": {"reasoning_tokens": 1190},
            },
        }
    )
    assert response.usage.output_tokens == 1200
    assert response.usage.reasoning_tokens == 1190


def test_usage_without_details_has_no_reasoning_tokens():
    response = normalize_openai_response(
        {
            "choices": [{"finish_reason": "stop", "message": {"content": "hi"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2},
        }
    )
    assert response.usage.reasoning_tokens == 0


def test_usage_addition_sums_reasoning_tokens():
    total = Usage(output_tokens=10, reasoning_tokens=4) + Usage(output_tokens=5, reasoning_tokens=1)
    assert (total.output_tokens, total.reasoning_tokens) == (15, 5)


# --------------------------------------------------------------------------- #
# Claude
# --------------------------------------------------------------------------- #
def test_claude_default_adds_nothing():
    assert claude_reasoning_fields("claude-opus-5-5", "default") == {}


@pytest.mark.parametrize("model_id", ["claude-opus-5-5", "claude-sonnet-5-5", "claude-opus-4-8"])
def test_claude_effort_levels_use_adaptive_thinking_and_effort(model_id):
    assert claude_reasoning_fields(model_id, "medium") == {
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "medium"},
    }
    # Claude has no "minimal"; the nearest level is sent and recorded as such.
    assert claude_reasoning_fields(model_id, "minimal")["output_config"] == {"effort": "low"}


def test_claude_none_turns_thinking_off_where_the_model_allows_it():
    assert claude_reasoning_fields("claude-opus-4-8", "none") == {
        "thinking": {"type": "disabled"}
    }
    assert claude_reasoning_fields("claude-sonnet-5-5", "none") == {
        "thinking": {"type": "between_tools"}
    }
    assert claude_reasoning_fields("claude-haiku-4-5", "none") == {
        "thinking": {"type": "disabled"}
    }


@pytest.mark.parametrize("model_id", ["claude-opus-5-5", "claude-fable-5-1", "claude-mythos-5-1"])
def test_claude_models_that_cannot_stop_thinking_say_so(model_id):
    with pytest.raises(ReasoningUnsupportedError, match="cannot turn thinking off"):
        claude_reasoning_fields(model_id, "none")


def test_claude_models_without_effort_control_refuse_a_level():
    with pytest.raises(ReasoningUnsupportedError, match="no effort control"):
        claude_reasoning_fields("claude-haiku-4-5", "low")


def _claude_provider(monkeypatch, captured):
    module = types.ModuleType("anthropic")
    for name in ("BadRequestError", "RateLimitError", "InternalServerError", "APIConnectionError"):
        setattr(module, name, type(name, (Exception,), {}))

    def create(**kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(
            id="msg",
            model="claude-opus-4-8",
            content=[types.SimpleNamespace(type="text", text="ok")],
            stop_reason="end_turn",
            usage=types.SimpleNamespace(input_tokens=1, output_tokens=1),
        )

    class Anthropic:
        def __init__(self, api_key=None):
            self.messages = types.SimpleNamespace(create=create)

    module.Anthropic = Anthropic
    monkeypatch.setitem(sys.modules, "anthropic", module)
    from hiveloom.models.claude import ClaudeProvider

    return ClaudeProvider(api_key="test")


def test_claude_provider_sends_and_records_the_mapping(monkeypatch):
    captured: dict[str, Any] = {}
    response = _claude_provider(monkeypatch, captured).complete(
        system="s",
        messages=[{"role": "user", "content": "x"}],
        tools=[],
        config=ModelConfig(id="claude-opus-4-8", reasoning="high"),
    )
    assert captured["extra_body"]["thinking"] == {"type": "adaptive"}
    assert captured["extra_body"]["output_config"] == {"effort": "high"}
    assert response.reasoning_sent == {
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "high"},
    }


def test_claude_provider_default_is_byte_for_byte_what_it_was(monkeypatch):
    captured: dict[str, Any] = {}
    response = _claude_provider(monkeypatch, captured).complete(
        system="s",
        messages=[{"role": "user", "content": "x"}],
        tools=[],
        config=ModelConfig(id="claude-opus-4-8"),
    )
    assert "extra_body" not in captured
    assert response.reasoning_sent == {}


def test_claude_params_beat_the_mapping(monkeypatch):
    captured: dict[str, Any] = {}
    _claude_provider(monkeypatch, captured).complete(
        system="s",
        messages=[{"role": "user", "content": "x"}],
        tools=[],
        config=ModelConfig(
            id="claude-opus-4-8", reasoning="low", params={"output_config": {"effort": "max"}}
        ),
    )
    assert captured["extra_body"]["output_config"] == {"effort": "max"}


# --------------------------------------------------------------------------- #
# The spec field and the provider registry
# --------------------------------------------------------------------------- #
def test_spec_default_is_default():
    assert SpecModelConfig().reasoning == "default"


def test_spec_rejects_an_unknown_level():
    with pytest.raises(ValueError):
        SpecModelConfig(reasoning="extreme")


def test_model_settings_are_frozen_from_evolution_including_reasoning():
    assert "model" in ALWAYS_FROZEN


def test_spec_rejects_a_level_on_a_host_with_no_control():
    with pytest.raises(ValueError, match="no reasoning control"):
        SpecModelConfig(provider="mistral", id="mistral-small-latest", reasoning="low")
    # "default" sends nothing, so it is always fine.
    SpecModelConfig(provider="mistral", id="mistral-small-latest", reasoning="default")


def test_spec_rejects_a_claude_level_the_model_cannot_take():
    with pytest.raises(ValueError, match="cannot turn thinking off"):
        SpecModelConfig(provider="claude", id="claude-opus-5-5", reasoning="none")


def test_builtin_styles():
    ext.ensure_environment_loaded()
    styles = {info.name: info.reasoning_style for info in ext.providers()}
    assert styles["openrouter"] == "openrouter"
    assert styles["vllm"] == "chat_template"
    assert styles["openai"] == "openai"
    assert styles["claude"] == "anthropic"
    assert styles["mistral"] == "none"
    # Every builtin has a style; unknown hosts default to openai.
    assert all(styles.values())


def test_models_yaml_declares_a_style(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HIVELOOM_HOME", str(tmp_path))
    ext.reset()
    (tmp_path / "models.yaml").write_text(
        """
providers:
  gateway:
    base_url: http://localhost:9999/v1
    open_catalog: true
    reasoning_style: chat_template
  plain:
    base_url: http://localhost:9998/v1
    open_catalog: true
""",
        encoding="utf-8",
    )
    info = {p.name: p for p in ext.providers()}
    assert info["gateway"].reasoning_style == "chat_template"
    assert info["plain"].reasoning_style == "openai"


def test_models_yaml_style_on_a_builtin_keeps_its_endpoint(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HIVELOOM_HOME", str(tmp_path))
    ext.reset()
    (tmp_path / "models.yaml").write_text(
        "providers:\n  mistral:\n    reasoning_style: openai\n", encoding="utf-8"
    )
    meta = ext.provider_info("mistral")
    assert meta.reasoning_style == "openai"
    assert meta.base_url == "https://api.mistral.ai/v1"
    SpecModelConfig(provider="mistral", id="mistral-small-latest", reasoning="low")


def test_models_yaml_rejects_an_unknown_style(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HIVELOOM_HOME", str(tmp_path))
    ext.reset()
    (tmp_path / "models.yaml").write_text(
        "providers:\n  bad:\n    base_url: http://x/v1\n    reasoning_style: sideways\n",
        encoding="utf-8",
    )
    assert "bad" not in ext.provider_names()
    assert any("reasoning_style" in e["error"] for e in ext.status()["errors"])


def test_register_provider_accepts_a_style():
    api = ext.ExtensionAPI(source="test")
    api.register_provider("mine", lambda ctx: None, reasoning_style="openrouter")
    assert ext.provider_info("mine").reasoning_style == "openrouter"
    with pytest.raises(ext.ExtensionError, match="reasoning_style"):
        api.register_provider("other", lambda ctx: None, reasoning_style="sideways")


# --------------------------------------------------------------------------- #
# Records and the empty-truncated diagnosis
# --------------------------------------------------------------------------- #
def _truncated_empty(reasoning_tokens: int = 1190) -> ModelResponse:
    return ModelResponse(
        text="",
        stop_reason="max_tokens",
        usage=Usage(input_tokens=10, output_tokens=1200, reasoning_tokens=reasoning_tokens),
        reasoning_sent={"reasoning_effort": "low"},
    )


def test_every_provider_call_records_stop_reason_and_emptiness(harness_dir: Path):
    construct.set_field(harness_dir, "loop.require_verification", "false")
    result = runner.run_harness(
        harness_dir, "go", provider=FakeModelProvider([text_response("42")])
    )
    call = result.provider_calls[0]
    assert call["stop_reason"] == "end_turn"
    assert call["output_empty"] is False
    assert call["reasoning_sent"] == {}


def test_a_turn_with_no_text_and_no_tool_calls_is_recorded_as_empty(harness_dir: Path):
    construct.set_field(harness_dir, "loop.require_verification", "false")
    result = runner.run_harness(
        harness_dir, "go", provider=FakeModelProvider([text_response("   ")])
    )
    assert result.provider_calls[0]["output_empty"] is True


def test_a_one_turn_run_that_reasoned_to_the_ceiling_says_so(harness_dir: Path):
    construct.set_field(harness_dir, "loop.require_verification", "false")
    construct.set_field(harness_dir, "loop.max_turns", "1")
    construct.set_field(harness_dir, "model.max_tokens", "1200")
    result = runner.run_harness(
        harness_dir, "go", provider=FakeModelProvider([_truncated_empty()])
    )
    # The existing machine-readable status carries it; no parallel field.
    assert result.status == "truncated"
    assert "hit model.max_tokens (1200) with no answer" in result.reason
    assert "1190 reasoning tokens" in result.reason
    assert "raise model.max_tokens or set model.reasoning" in result.reason
    call = result.provider_calls[0]
    assert call["stop_reason"] == "max_tokens"
    assert call["output_empty"] is True
    assert call["usage"]["reasoning_tokens"] == 1190
    assert call["reasoning_sent"] == {"reasoning_effort": "low"}


def test_the_diagnosis_survives_a_validator_that_cannot_pass_on_nothing(harness_dir: Path):
    construct.set_field(harness_dir, "loop.max_turns", "1")
    construct.set_field(harness_dir, "model.max_tokens", "1200")
    construct.add_validator(harness_dir, builtin="regex_match", pattern="42")
    result = runner.run_harness(
        harness_dir, "go", provider=FakeModelProvider([_truncated_empty()])
    )
    assert result.status == "truncated"
    assert "no answer" in result.reason
    assert result.verdicts and not result.verdicts[0].passed


# --------------------------------------------------------------------------- #
# The static warning
# --------------------------------------------------------------------------- #
def _reasoning_harness(harness_dir: Path, max_tokens: int, reasoning: str = "default"):
    construct.set_value(
        harness_dir,
        "model",
        {
            "provider": "gemini",
            "id": "gemini-2.5-flash",
            "max_tokens": max_tokens,
            "reasoning": reasoning,
        },
    )


def test_warns_when_a_reasoning_model_has_a_small_budget(harness_dir: Path):
    _reasoning_harness(harness_dir, 1200)
    warnings = spec_warnings(validate_harness(harness_dir))
    assert len(warnings) == 1
    assert "model.reasoning" in warnings[0] and "1200" in warnings[0]


def test_no_warning_with_room_or_with_reasoning_set(harness_dir: Path):
    _reasoning_harness(harness_dir, 8192)
    assert spec_warnings(validate_harness(harness_dir)) == []
    _reasoning_harness(harness_dir, 1200, reasoning="low")
    assert spec_warnings(validate_harness(harness_dir)) == []


def test_no_warning_for_an_unknown_or_non_reasoning_model(harness_dir: Path):
    construct.set_value(harness_dir, "model.max_tokens", 1200)
    assert spec_warnings(validate_harness(harness_dir)) == []


def test_models_yaml_can_mark_a_model_as_reasoning(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HIVELOOM_HOME", str(tmp_path))
    ext.reset()
    (tmp_path / "models.yaml").write_text(
        """
providers:
  openrouter:
    models:
      - id: z-ai/glm-4.6
        input_cost_per_mtok: 0.5
        output_cost_per_mtok: 1.5
        reasoning: true
""",
        encoding="utf-8",
    )
    assert ext.model_info("z-ai/glm-4.6").reasoning is True


def test_validate_and_dry_run_report_the_warning(harness_dir: Path):
    from typer.testing import CliRunner

    _reasoning_harness(harness_dir, 1200)
    cli = CliRunner()
    validated = cli.invoke(app, ["validate", str(harness_dir), "--json"])
    assert validated.exit_code == 0
    assert "model.reasoning" in json.loads(validated.stdout)["warnings"][0]

    dry = cli.invoke(app, ["run", str(harness_dir), "--input", "x", "--dry-run", "--json"])
    assert dry.exit_code == 0
    assert "model.reasoning" in json.loads(dry.stdout)["warnings"][0]


# --------------------------------------------------------------------------- #
# Compatibility edges
# --------------------------------------------------------------------------- #
def test_the_default_level_leaves_the_dumped_spec_and_its_hash_alone(harness_dir: Path):
    from hiveloom.logging.trace import spec_version_hash
    from hiveloom.spec.loader import dump_spec

    spec = validate_harness(harness_dir)
    assert "reasoning" not in dump_spec(spec)
    low = spec.model_copy(update={"model": spec.model.model_copy(update={"reasoning": "low"})})
    assert "reasoning: low" in dump_spec(low)
    assert spec_version_hash(low) != spec_version_hash(spec)


def test_an_unlisted_claude_id_takes_effort_rather_than_failing_validation():
    assert claude_reasoning_fields("claude-opus-9", "high") == {
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "high"},
    }


def test_a_style_declared_on_a_builtin_reaches_the_built_provider(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HIVELOOM_HOME", str(tmp_path))
    monkeypatch.setenv("MISTRAL_API_KEY", "sk-test")
    ext.reset()
    (tmp_path / "models.yaml").write_text(
        "providers:\n  mistral:\n    reasoning_style: openai\n", encoding="utf-8"
    )
    assert ext.build_provider("mistral")._reasoning_style == "openai"


def test_a_switch_to_a_model_that_cannot_apply_the_level_is_refused(tmp_path: Path):
    from hiveloom.models.router import ModelRouter

    fake = FakeModelProvider([])
    router = ModelRouter.create(
        tmp_path,
        ModelConfig(id="claude-opus-4-8", provider="claude", reasoning="high"),
        fake,
        providers={"mistral": fake},
    )
    with pytest.raises(ReasoningUnsupportedError):
        router.switch(model="mistral-small-latest", provider="mistral")
    assert router.config.id == "claude-opus-4-8"
    assert router.switch(model="claude-opus-4-7", provider="claude") is not None
    assert router.config.reasoning == "high"


def test_claude_repointed_at_a_gateway_takes_the_openai_style(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HIVELOOM_HOME", str(tmp_path))
    ext.reset()
    (tmp_path / "models.yaml").write_text(
        "providers:\n  claude:\n    base_url: http://localhost:9999/v1\n", encoding="utf-8"
    )
    assert ext.provider_info("claude").reasoning_style == "openai"


def test_claude_structured_output_keeps_the_mapped_effort(monkeypatch):
    captured: dict[str, Any] = {}
    fmt = {"type": "json_schema", "schema": {"type": "object"}}
    _claude_provider(monkeypatch, captured).complete(
        system="s",
        messages=[{"role": "user", "content": "x"}],
        tools=[],
        config=ModelConfig(
            id="claude-opus-4-8", reasoning="low", params={"output_config": {"format": fmt}}
        ),
    )
    assert captured["extra_body"]["output_config"] == {"effort": "low", "format": fmt}
