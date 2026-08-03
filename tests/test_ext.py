"""Tests for the extension registry: registration, discovery, and providers."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hiveloom import catalog, construct, ext, runner, trust
from hiveloom.cli import app
from hiveloom.errors import CatalogError, SpecError
from hiveloom.models.fake import FakeModelProvider, text_response, tool_response
from hiveloom.models.provider import Usage
from hiveloom.spec.loader import load_spec, spec_from_dict
from hiveloom.spec.schema import HarnessSpec, ModelConfig
from hiveloom.tools.registry import Tool, build_registry

cli_runner = CliRunner()


class _EchoTool(Tool):
    def __init__(self, prefix: str):
        self.name = "echo"
        self.description = "Echo the input back."
        self.tags = ["test"]
        self.input_schema = {"type": "object", "properties": {"text": {"type": "string"}}}
        self._prefix = prefix

    def run(self, text: str = "", **_):
        return f"{self._prefix}{text}"


def _register_echo(hive: ext.ExtensionAPI) -> None:
    hive.register_tool(
        "echo",
        lambda params, ctx: _EchoTool(params.get("prefix", "")),
        description="Echo the input back.",
        tags=["test"],
        params=[{"name": "prefix", "type": "str"}],
    )


# --------------------------------------------------------------------------- #
# Registration API
# --------------------------------------------------------------------------- #
def test_registered_tool_appears_in_catalog_with_source():
    _register_echo(ext.ExtensionAPI(source="test:inline"))
    entry = catalog.BUILTIN_TOOLS["echo"]
    assert entry.source == "test:inline"
    assert entry.params[0].name == "prefix"


def test_registered_tool_validates_in_spec_and_builds(tmp_path: Path):
    _register_echo(ext.ExtensionAPI(source="test:inline"))
    spec = HarnessSpec(
        name="h",
        description="d",
        system_prompt="s",
        tools=[{"builtin": "echo", "prefix": "> "}],
    )
    registry = build_registry(spec, tmp_path)
    result = registry.get("echo").run(text="hi")
    assert result == "> hi"


def test_register_function_tool_derives_schema():
    api = ext.ExtensionAPI(source="test:fn")

    def shout(text: str) -> str:
        """Uppercase the input."""
        return text.upper()

    api.register_function_tool(shout)
    built = ext.build("tools", "shout", {}, ext.BuildContext())
    assert built.run(text="hi") == "HI"
    assert "text" in built.input_schema["properties"]


def test_duplicate_registration_rejected():
    api = ext.ExtensionAPI(source="test:dup")
    with pytest.raises(ext.ExtensionError, match="already registered"):
        api.register_tool("file_read", lambda p, c: None, description="clash")


def test_registered_guardrail_and_validator_build(tmp_path: Path):
    from hiveloom.guardrails.base import Block, Guardrail
    from hiveloom.verify.base import VerdictResult, Verifier

    class _NoFoo(Guardrail):
        name = "no_foo"

        def on_output(self, state, output):
            return Block("foo!") if "foo" in output else super().on_output(state, output)

    class _MinLen(Verifier):
        name = "min_len"

        def __init__(self, n: int):
            self._n = n

        def validate(self, run_output, run_context):
            ok = len(run_output) >= self._n
            return VerdictResult(passed=ok, feedback="" if ok else "too short")

    api = ext.ExtensionAPI(source="test:gv")
    api.register_guardrail("no_foo", lambda p, c: _NoFoo(), description="Block foo.")
    api.register_validator(
        "min_len",
        lambda p, c: _MinLen(p["n"]),
        description="Minimum output length.",
        params=[{"name": "n", "type": "int", "required": True}],
    )

    spec = HarnessSpec(
        name="h",
        description="d",
        system_prompt="s",
        guardrails=[{"builtin": "no_foo"}],
        verify={"validators": [{"builtin": "min_len", "n": 3}]},
    )
    from hiveloom.guardrails.builtin import build_guardrails
    from hiveloom.verify.builtin import build_verifiers

    guardrails = build_guardrails(spec, build_registry(spec, tmp_path), tmp_path)
    assert any(g.name == "no_foo" for g in guardrails)
    verifiers = build_verifiers(spec, tmp_path)
    verdict = [v for v in verifiers if v.name == "min_len"][0].validate("hi", {})
    assert not verdict.passed


def test_unknown_catalog_entry_build_raises_actionable_error():
    with pytest.raises(CatalogError, match="extension pack"):
        ext.build("tools", "nope", {}, ext.BuildContext())


def test_reset_restores_builtins_only():
    _register_echo(ext.ExtensionAPI(source="test:inline"))
    assert "echo" in catalog.BUILTIN_TOOLS
    ext.reset()
    assert "echo" not in catalog.BUILTIN_TOOLS
    assert "file_read" in catalog.BUILTIN_TOOLS
    # Builtin factories survive a reset.
    spec = HarnessSpec(name="h", description="d", system_prompt="s",
                       tools=[{"builtin": "file_read"}])
    assert build_registry(spec, Path(".")).get("file_read") is not None


# --------------------------------------------------------------------------- #
# Environment discovery (user dir) and error collection
# --------------------------------------------------------------------------- #
_GOOD_EXT = """
def hiveloom_extension(hive):
    def ping() -> str:
        \"\"\"Reply with pong.\"\"\"
        return "pong"
    hive.register_function_tool(ping)
"""

_BROKEN_EXT = "this is not python ==="


def _user_ext_dir(tmp_home: Path) -> Path:
    d = tmp_home / "extensions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_user_dir_extensions_load_and_errors_are_collected(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HIVELOOM_HOME", str(tmp_path))
    ext.reset()
    ext_dir = _user_ext_dir(tmp_path)
    (ext_dir / "good.py").write_text(_GOOD_EXT, encoding="utf-8")
    (ext_dir / "broken.py").write_text(_BROKEN_EXT, encoding="utf-8")

    ext.ensure_environment_loaded()
    assert "ping" in catalog.BUILTIN_TOOLS
    status = ext.status()
    assert any(e["source"] == "user:broken.py" for e in status["errors"])
    assert any(s["source"] == "user:good.py" for s in status["sources"])


def test_environment_loads_once(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HIVELOOM_HOME", str(tmp_path))
    ext.reset()
    (_user_ext_dir(tmp_path) / "good.py").write_text(_GOOD_EXT, encoding="utf-8")
    ext.ensure_environment_loaded()
    ext.ensure_environment_loaded()  # idempotent: no duplicate-registration error
    assert "ping" in catalog.BUILTIN_TOOLS


def test_untrusted_user_extensions_are_skipped(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HIVELOOM_HOME", str(tmp_path))
    monkeypatch.setenv("HIVELOOM_TRUST", "never")
    ext.reset()
    ext_dir = _user_ext_dir(tmp_path)
    (ext_dir / "good.py").write_text(_GOOD_EXT, encoding="utf-8")
    trust.revoke_trust(ext_dir)

    ext.ensure_environment_loaded()

    assert "ping" not in catalog.BUILTIN_TOOLS
    assert any("not trusted" in error["error"] for error in ext.status()["errors"])


# --------------------------------------------------------------------------- #
# Harness-declared extensions
# --------------------------------------------------------------------------- #
def test_harness_extensions_field_loads_before_validation(harness_dir: Path):
    (harness_dir / "ext").mkdir()
    (harness_dir / "ext" / "mine.py").write_text(_GOOD_EXT, encoding="utf-8")
    construct.set_field(harness_dir, "extensions", '["ext/mine.py"]')
    construct.add_tool(harness_dir, builtin="ping")

    ext.reset()  # a fresh process must be able to load the folder cold
    spec = load_spec(harness_dir)
    assert any(getattr(t, "builtin", None) == "ping" for t in spec.tools)

    provider = FakeModelProvider(
        [tool_response("ping", {}), text_response("done")]
    )
    result = runner.run_harness(harness_dir, "go", provider=provider)
    assert result.status == "success"


def test_missing_harness_extension_is_spec_error(harness_dir: Path):
    raw = {"name": "h", "description": "d", "system_prompt": "s",
           "extensions": ["ext/nope.py"]}
    with pytest.raises(SpecError, match="nope.py"):
        spec_from_dict(raw, base_dir=harness_dir)


def test_extensions_path_is_always_frozen_for_evolution(harness_dir: Path):
    from hiveloom.evolve.evolver import MutationProposal, YamlChange, gate

    spec = load_spec(harness_dir)
    proposal = MutationProposal(
        yaml_changes=[YamlChange(path="extensions", value=["evil.py"], rationale="")]
    )
    result = gate(spec, proposal)
    assert not result.accepted
    assert result.rejected[0]["reason"] == "frozen path"


# --------------------------------------------------------------------------- #
# Providers & models.yaml
# --------------------------------------------------------------------------- #
_MODELS_YAML = """
providers:
  localllm:
    api: openai_compat
    base_url: http://localhost:9999/v1
    models:
      - id: tiny-model
        input_cost_per_mtok: 0.1
        output_cost_per_mtok: 0.2
"""


def test_models_yaml_registers_provider_and_pricing(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HIVELOOM_HOME", str(tmp_path))
    ext.reset()
    (tmp_path / "models.yaml").write_text(_MODELS_YAML, encoding="utf-8")

    assert "localllm" in ext.provider_names()
    assert ext.model_pricing("tiny-model") == (0.1, 0.2)
    # The spec now accepts the provider by name.
    config = ModelConfig(provider="localllm", id="tiny-model")
    assert config.provider == "localllm"


def test_models_yaml_without_pricing_uses_conservative_fallback(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HIVELOOM_HOME", str(tmp_path))
    ext.reset()
    (tmp_path / "models.yaml").write_text(
        """
providers:
  remote:
    api: openai_compat
    base_url: https://api.example.test/v1
    models:
      - id: unknown-price
""",
        encoding="utf-8",
    )

    assert ext.model_pricing("unknown-price") == (1.0, 5.0)
    assert any("omits pricing" in error["error"] for error in ext.status()["errors"])


def test_unknown_provider_rejected_in_spec():
    with pytest.raises(ValueError, match="unknown model provider"):
        ModelConfig(provider="who-dis")


def test_unknown_or_wrong_provider_model_rejected_in_spec(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HIVELOOM_HOME", str(tmp_path))
    ext.reset()
    (tmp_path / "models.yaml").write_text(_MODELS_YAML, encoding="utf-8")

    with pytest.raises(ValueError, match="unknown model id"):
        ModelConfig(id="does-not-exist")
    with pytest.raises(ValueError, match="belongs to provider 'localllm'"):
        ModelConfig(provider="claude", id="tiny-model")


def test_model_and_runtime_numeric_bounds_are_rejected():
    with pytest.raises(ValueError, match="less than or equal to 32768"):
        ModelConfig(max_tokens=32_769)
    with pytest.raises(ValueError, match="less than or equal to 1000000"):
        HarnessSpec(
            name="h",
            description="d",
            system_prompt="s",
            context={"max_input_tokens": 1_000_001},
        )
    with pytest.raises(ValueError, match="max_cost_usd.value"):
        HarnessSpec(
            name="h",
            description="d",
            system_prompt="s",
            guardrails=[{"builtin": "max_cost_usd", "value": 10_001}],
        )


def test_estimated_cost_uses_registry_pricing(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HIVELOOM_HOME", str(tmp_path))
    ext.reset()
    (tmp_path / "models.yaml").write_text(_MODELS_YAML, encoding="utf-8")

    from hiveloom.models.provider import Usage

    provider = FakeModelProvider([])
    cost = provider.estimated_cost(
        Usage(input_tokens=1_000_000, output_tokens=1_000_000), "tiny-model"
    )
    assert cost == pytest.approx(0.3)


def test_bad_models_yaml_is_collected_not_fatal(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HIVELOOM_HOME", str(tmp_path))
    ext.reset()
    (tmp_path / "models.yaml").write_text("providers: {broken: {}}", encoding="utf-8")
    assert "claude" in ext.provider_names()  # still bootstraps
    assert any(e["source"] == "models.yaml" for e in ext.status()["errors"])


# --------------------------------------------------------------------------- #
# OpenAI-compatible provider
# --------------------------------------------------------------------------- #
def test_openai_compat_message_conversion():
    from hiveloom.models.openai_compat import _to_openai_messages, _to_openai_tool

    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "checking"},
                {"type": "tool_use", "id": "c1", "name": "echo", "input": {"text": "x"}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "c1", "content": "x", "is_error": False}
            ],
        },
    ]
    out = _to_openai_messages("sys", messages)
    assert out[0] == {"role": "system", "content": "sys"}
    assert out[2]["tool_calls"][0]["function"]["name"] == "echo"
    assert out[3] == {"role": "tool", "tool_call_id": "c1", "content": "x"}

    tool = _to_openai_tool({"name": "echo", "description": "d", "input_schema": {"type": "object"}})
    assert tool["function"]["parameters"] == {"type": "object"}


def test_openai_compat_normalizes_tool_calls(monkeypatch):
    from hiveloom.models import openai_compat
    from hiveloom.models.provider import ModelConfig as MC

    response = {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c9",
                            "type": "function",
                            "function": {"name": "echo", "arguments": '{"text": "y"}'},
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    provider = openai_compat.OpenAICompatProvider("http://x/v1", api_key="k")
    captured: dict = {}

    def fake_post(self, path, payload):
        captured["path"] = path
        captured["payload"] = payload
        return response

    monkeypatch.setattr(openai_compat.OpenAICompatProvider, "_post", fake_post)
    result = provider.complete(
        system="s",
        messages=[{"role": "user", "content": "go"}],
        tools=[{"name": "echo", "description": "d", "input_schema": {"type": "object"}}],
        config=MC(id="tiny-model"),
    )
    assert captured["path"] == "/chat/completions"
    assert captured["payload"]["tools"][0]["function"]["name"] == "echo"
    assert result.stop_reason == "tool_use"
    assert result.tool_calls[0].input == {"text": "y"}
    assert result.usage.input_tokens == 10


def test_openai_compat_estimates_usage_when_server_omits_it(monkeypatch):
    from hiveloom.models import openai_compat
    from hiveloom.models.provider import ModelConfig as MC

    provider = openai_compat.OpenAICompatProvider("http://x/v1")
    monkeypatch.setattr(
        openai_compat.OpenAICompatProvider,
        "_post",
        lambda *_args: {"choices": [{"finish_reason": "stop", "message": {"content": "done"}}]},
    )

    result = provider.complete(
        system="system prompt",
        messages=[{"role": "user", "content": "a request"}],
        tools=[],
        config=MC(id="tiny-model"),
    )

    assert result.usage.input_tokens > 0
    assert result.usage.output_tokens > 0


def test_openai_compat_retries_transient_transport_failure(monkeypatch):
    from urllib import error as urlerror

    from hiveloom.models import openai_compat

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"ok": true}'

    attempts = 0

    def flaky_urlopen(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise urlerror.URLError("temporary failure")
        return Response()

    sleeps: list[float] = []
    monkeypatch.setattr(openai_compat.urlrequest, "urlopen", flaky_urlopen)
    provider = openai_compat.OpenAICompatProvider("http://x/v1", sleep=sleeps.append)

    assert provider._post("/chat/completions", {}) == {"ok": True}
    assert attempts == 2
    assert sleeps == [1.0]


def test_openai_compat_error_counts_attempts_actually_made(monkeypatch):
    """Regression for issue #7: a 400 is never retried, so the error must not
    claim the retry budget was spent."""
    from urllib import error as urlerror

    from hiveloom.models import openai_compat

    calls = 0

    def bad_request(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise urlerror.HTTPError("http://x/v1", 400, "Bad Request", {}, io.BytesIO(b"nope"))

    monkeypatch.setattr(openai_compat.urlrequest, "urlopen", bad_request)
    provider = openai_compat.OpenAICompatProvider("http://x/v1", sleep=lambda _s: None)

    with pytest.raises(RuntimeError, match=r"failed on attempt 1: "):
        provider._post("/chat/completions", {})
    assert calls == 1


def test_openai_compat_error_reports_the_full_budget_when_it_is_spent(monkeypatch):
    from urllib import error as urlerror

    from hiveloom.models import openai_compat

    def always_down(*_args, **_kwargs):
        raise urlerror.URLError("down")

    monkeypatch.setattr(openai_compat.urlrequest, "urlopen", always_down)
    provider = openai_compat.OpenAICompatProvider("http://x/v1", sleep=lambda _s: None)

    with pytest.raises(RuntimeError, match=r"failed on attempt 4: "):
        provider._post("/chat/completions", {})


def test_claude_retries_overload_and_normalizes_response():
    """Exercise the provider's retry path without importing the Anthropic SDK."""
    from types import SimpleNamespace

    from hiveloom.models.claude import ClaudeProvider

    class RetryableError(Exception):
        pass

    attempts = 0

    def create(**_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RetryableError("overloaded")
        return SimpleNamespace(
            content=[
                SimpleNamespace(type="text", text="checking"),
                SimpleNamespace(type="tool_use", id="c1", name="echo", input={"x": 1}),
            ],
            usage=SimpleNamespace(input_tokens=7, output_tokens=3),
            stop_reason="tool_use",
        )

    provider = ClaudeProvider.__new__(ClaudeProvider)
    provider._anthropic = SimpleNamespace(
        RateLimitError=RetryableError,
        InternalServerError=RetryableError,
        APIConnectionError=RetryableError,
        OverloadedError=RetryableError,
    )
    provider._client = SimpleNamespace(messages=SimpleNamespace(create=create))
    sleeps: list[float] = []
    provider._sleep = sleeps.append

    raw = provider._call_with_backoff(model="claude-test", messages=[])
    result = provider._normalize(raw)

    assert attempts == 2
    assert sleeps == [1.0]
    assert result.text == "checking"
    assert result.tool_calls[0].input == {"x": 1}
    assert result.usage.input_tokens == 7


# --------------------------------------------------------------------------- #
# CLI surface
# --------------------------------------------------------------------------- #
def test_extensions_command_json(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HIVELOOM_HOME", str(tmp_path))
    ext.reset()
    (_user_ext_dir(tmp_path) / "good.py").write_text(_GOOD_EXT, encoding="utf-8")
    r = cli_runner.invoke(app, ["extensions", "--json"])
    assert r.exit_code == 0
    payload = json.loads(r.stdout)
    assert payload["ok"] is True
    assert "claude" in payload["providers"]
    assert any(s["source"] == "user:good.py" for s in payload["sources"])


def test_catalog_json_includes_source():
    r = cli_runner.invoke(app, ["catalog", "tools", "--json"])
    payload = json.loads(r.stdout)
    assert all(e["source"] == "builtin" for e in payload["entries"])


# --------------------------------------------------------------------------- #
# Builtin multi-lab providers
# --------------------------------------------------------------------------- #
def test_major_labs_are_builtin_providers():
    """Every lab hiveloom advertises must be usable without any user config."""
    names = ext.provider_names()
    for expected in (
        "claude", "openai", "gemini", "mistral", "deepseek", "xai",
        "groq", "openrouter", "together", "fireworks", "ollama", "vllm",
    ):
        assert expected in names, f"{expected} is not a builtin provider"


def test_builtin_openai_compat_providers_declare_endpoint_and_key():
    for info in ext.providers():
        if info.name == "claude":
            assert info.api == "anthropic"
            continue
        assert info.base_url.startswith("http"), info.name
        # Local servers need no key; every hosted one must name its variable,
        # since that string is what `hiveloom models` tells the user to set.
        if "localhost" not in info.base_url:
            assert info.api_key_env, info.name


def test_open_catalog_provider_accepts_an_unregistered_model_id():
    """A model released after this hiveloom version must still be usable."""
    assert ext.model_info("gpt-6-turbo-not-real") is None
    config = ModelConfig(provider="openai", id="gpt-6-turbo-not-real")
    assert config.id == "gpt-6-turbo-not-real"


def test_closed_catalog_provider_still_rejects_a_typo():
    with pytest.raises(ValueError, match="unknown model id"):
        ModelConfig(provider="claude", id="claude-hiaku-4-5")


def test_unknown_hosted_model_falls_back_to_conservative_pricing():
    """Unknown + hosted must not be free, or a budget guardrail under-counts."""
    assert ext.model_pricing("gpt-6-turbo-not-real", provider="openai") is None
    provider = FakeModelProvider([])
    cost = provider.estimated_cost(
        Usage(input_tokens=1_000_000, output_tokens=1_000_000),
        "gpt-6-turbo-not-real",
        "openai",
    )
    assert cost == pytest.approx(6.0)  # the (1.00, 5.00) conservative fallback


def test_unknown_local_model_is_priced_free():
    """A local server costs nothing; charging Haiku rates would trip budgets."""
    assert ext.model_pricing("qwen3:8b", provider="ollama") == (0.0, 0.0)
    provider = FakeModelProvider([])
    cost = provider.estimated_cost(
        Usage(input_tokens=1_000_000, output_tokens=1_000_000), "qwen3:8b", "ollama"
    )
    assert cost == 0.0


def test_models_yaml_can_override_a_builtin_provider(monkeypatch, tmp_path: Path):
    """A corporate gateway must be able to take over a builtin lab name."""
    monkeypatch.setenv("HIVELOOM_HOME", str(tmp_path))
    ext.reset()
    (tmp_path / "models.yaml").write_text(
        """
providers:
  openai:
    base_url: https://gateway.internal.test/v1
    api_key_env: INTERNAL_KEY
    models:
      - id: house-model
        input_cost_per_mtok: 0
        output_cost_per_mtok: 0
""",
        encoding="utf-8",
    )

    info = ext.provider_info("openai")
    assert info.base_url == "https://gateway.internal.test/v1"
    assert info.source == "models.yaml"
    assert ext.model_pricing("house-model") == (0.0, 0.0)
    assert not ext.status()["errors"]


def test_models_yaml_can_extend_a_builtin_without_restating_the_endpoint(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setenv("HIVELOOM_HOME", str(tmp_path))
    ext.reset()
    (tmp_path / "models.yaml").write_text(
        """
providers:
  openai:
    models:
      - id: gpt-4o
        input_cost_per_mtok: 1.11
        output_cost_per_mtok: 2.22
""",
        encoding="utf-8",
    )

    # Endpoint survives; the corrected price wins over the shipped one.
    assert ext.provider_info("openai").base_url == "https://api.openai.com/v1"
    assert ext.model_pricing("gpt-4o") == (1.11, 2.22)


def test_models_yaml_extend_of_an_unknown_provider_is_an_error(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HIVELOOM_HOME", str(tmp_path))
    ext.reset()
    (tmp_path / "models.yaml").write_text(
        """
providers:
  nosuchlab:
    models:
      - id: whatever
        input_cost_per_mtok: 0
        output_cost_per_mtok: 0
""",
        encoding="utf-8",
    )

    assert "nosuchlab" not in ext.provider_names()
    assert any("base_url is required" in e["error"] for e in ext.status()["errors"])


def test_models_command_lists_providers_and_hides_key_values(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-super-secret-value")
    r = cli_runner.invoke(app, ["models", "--json"])
    assert r.exit_code == 0
    payload = json.loads(r.stdout)
    by_name = {p["name"]: p for p in payload["providers"]}
    assert by_name["openai"]["api_key_set"] is True
    assert by_name["gemini"]["api_key_set"] is False
    assert by_name["ollama"]["open_catalog"] is True
    # The output is routinely pasted into agents and issues.
    assert "sk-super-secret-value" not in r.stdout


def test_models_command_rejects_an_unknown_provider():
    r = cli_runner.invoke(app, ["models", "nosuchlab"])
    assert r.exit_code != 0
