"""The last check before content leaves the machine for a model provider."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hiveloom import construct, runner
from hiveloom.egress import CREDENTIAL_PATTERNS, EgressFilter, policy_name
from hiveloom.logging.journal import read_events
from hiveloom.logging.trace import payload_hash
from hiveloom.models.fake import FakeModelProvider, text_response, tool_response
from hiveloom.spec.schema import EgressConfig, RedactionConfig
from hiveloom.tools.builtin import HttpGetTool, _validate_public_http_url
from hiveloom.tools.registry import ToolError

LEAKY_TOOL = '''
from hiveloom.tools import tool


@tool(description="Read the deployment notes.")
def notes(label: str = "") -> str:
    return "deploy with " + "AKIA" + "QRSTUVWXYZ012345" + " and retry on failure"
'''


def _filter(**overrides) -> EgressFilter:
    redact = overrides.pop("redact", None)
    return EgressFilter(EgressConfig(**overrides), redact)


def _harness(tmp_path: Path, **fields: str) -> Path:
    directory = tmp_path / "h"
    construct.init_harness(directory, name="egress-harness", task="Deploy the thing.")
    construct.set_field(directory, "loop.require_verification", "false")
    (directory / "tools").mkdir(exist_ok=True)
    (directory / "tools" / "notes.py").write_text(LEAKY_TOOL)
    construct.add_tool(directory, code="tools/notes.py:notes", description="Read notes.")
    for path, value in fields.items():
        construct.set_field(directory, path, value)
    return directory


def _sent(provider: FakeModelProvider) -> str:
    return json.dumps([{"s": c["system"], "m": c["messages"]} for c in provider.calls])


# --------------------------------------------------------------------------- #
# The filter
# --------------------------------------------------------------------------- #
def test_a_clean_request_passes_through_untouched(tmp_path: Path):
    messages = [{"role": "user", "content": "summarize the report"}]
    verdict = _filter().apply("be helpful", messages)

    assert verdict.clean
    assert verdict.messages is messages  # not even copied


def test_credential_shapes_are_redacted_by_default():
    secret = "AKIA" + "ABCDEFGHIJKLMNOP"
    verdict = _filter().apply("", [{"role": "user", "content": f"key is {secret}"}])

    assert not verdict.clean
    assert secret not in json.dumps(verdict.messages)
    assert "[REDACTED]" in json.dumps(verdict.messages)
    assert verdict.findings == [{"pattern": "aws_access_key_id", "count": 1}]


def test_every_documented_credential_shape_is_recognised():
    samples = {
        "private_key_block": "-----BEGIN RSA PRIVATE KEY-----",
        "aws_access_key_id": "AKIA" + "ABCDEFGHIJKLMNOP",
        "anthropic_api_key": "sk-ant-" + "a" * 30,
        "openai_api_key": "sk-" + "b" * 40,
        "github_token": "ghp_" + "c" * 36,
        "slack_token": "xoxb-" + "1234567890-abcdef",
        "google_api_key": "AIza" + "d" * 35,
        "jwt": "eyJhbGciOiJI.eyJzdWIiOiIx.SflKxwRJSM",
        "bearer_header": "Authorization: Bearer " + "e" * 20,
    }
    assert set(samples) == {name for name, _ in CREDENTIAL_PATTERNS}
    for name, sample in samples.items():
        verdict = _filter().apply("", [{"role": "user", "content": sample}])
        assert [f["pattern"] for f in verdict.findings] == [name], sample


def test_the_harnesss_own_redact_patterns_apply_on_the_way_out_too():
    # A pattern that scrubs the journal but not the request would protect the
    # record of the leak rather than preventing it.
    verdict = _filter(redact=[r"CUST-\d+"]).apply(
        "", [{"role": "user", "content": "account CUST-4821 is overdue"}]
    )
    assert "CUST-4821" not in json.dumps(verdict.messages)
    assert verdict.findings[0]["pattern"] == "logging.redact[0]"


def test_block_mode_refuses_rather_than_rewriting():
    secret = "ghp_" + "z" * 36
    verdict = _filter(mode="block").apply("", [{"role": "user", "content": secret}])

    assert verdict.blocked
    assert verdict.messages[0]["content"] == secret  # untouched; the call is refused


def test_findings_never_carry_the_matched_text():
    secret = "AKIA" + "SECRETSECRET1234"
    verdict = _filter().apply("", [{"role": "user", "content": secret}])
    assert secret not in json.dumps(verdict.findings)
    assert secret not in verdict.summary()


def test_nested_content_blocks_are_rewritten_in_place():
    secret = "sk-ant-" + "q" * 30
    messages = [
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "c1", "content": secret}],
        }
    ]
    verdict = _filter().apply("", messages)

    assert verdict.messages[0]["content"][0]["type"] == "tool_result"
    assert secret not in json.dumps(verdict.messages)


def test_tool_schemas_are_part_of_the_screened_request():
    secret = "ghp_" + "t" * 36
    tools = [{"name": "deploy", "description": f"credential: {secret}"}]

    verdict = _filter().apply("", [], tools)

    assert secret not in json.dumps(verdict.tools)
    assert "[REDACTED]" in json.dumps(verdict.tools)


def test_dictionary_keys_are_screened_too():
    secret = "CUST-4821"
    verdict = _filter(redact=[r"CUST-\d+"]).apply(
        "", [{"role": "user", "content": {secret: "value"}}]
    )

    assert secret not in json.dumps(verdict.messages)


def test_structured_logging_redaction_applies_outbound():
    redaction = RedactionConfig(keys=["token"], paths=["payload.secret"])
    verdict = EgressFilter(EgressConfig(), redaction).apply(
        "",
        [
            {
                "role": "user",
                "content": {
                    "token": "known-by-key",
                    "payload": {"secret": "known-by-path"},
                },
            }
        ],
    )

    serialized = json.dumps(verdict.messages)
    assert "known-by-key" not in serialized
    assert "known-by-path" not in serialized


def test_the_filter_can_be_switched_off():
    secret = "AKIA" + "ABCDEFGHIJKLMNOP"
    verdict = _filter(mode="off").apply("", [{"role": "user", "content": secret}])
    assert verdict.clean
    assert policy_name(EgressConfig(mode="off")) == "off"


def test_an_empty_noncredential_policy_is_reported_as_inactive():
    config = EgressConfig(detect_credentials=False)
    assert not EgressFilter(config).enabled
    assert policy_name(config) == "off"


# --------------------------------------------------------------------------- #
# In a run
# --------------------------------------------------------------------------- #
def test_a_credential_in_a_tool_result_never_reaches_the_provider(tmp_path: Path):
    harness = _harness(tmp_path)
    provider = FakeModelProvider(
        [tool_response("notes", {}, call_id="c1"), text_response("deployed")]
    )
    result = runner.run_harness(
        harness, "go", provider=provider, literal_input=True, ingest=False
    )

    assert result.status == "success"
    assert "AKIAQRSTUVWXYZ012345" not in _sent(provider)
    assert "[REDACTED]" in _sent(provider)

    event = next(
        e for e in read_events(result.trace_path) if e["type"] == "provider_egress_redacted"
    )
    assert event["payload"]["patterns"] == [{"pattern": "aws_access_key_id", "count": 1}]
    # The journal records that something matched, never what it was.
    assert "AKIAQRSTUVWXYZ012345" not in json.dumps(event["payload"])


def test_block_mode_halts_the_run_instead_of_sending(tmp_path: Path):
    harness = _harness(tmp_path, **{"egress.mode": "block"})
    provider = FakeModelProvider(
        [tool_response("notes", {}, call_id="c1"), text_response("deployed")]
    )
    result = runner.run_harness(
        harness, "go", provider=provider, literal_input=True, ingest=False
    )

    assert result.status == "guardrail_halt"
    assert "blocked before it left the machine" in result.reason
    assert "AKIAQRSTUVWXYZ012345" not in _sent(provider)
    blocked = next(
        e for e in read_events(result.trace_path) if e["type"] == "provider_egress_blocked"
    )
    assert blocked["payload"]["mode"] == "block"


def test_the_journal_still_records_what_the_tool_actually_returned(tmp_path: Path):
    # Egress rewrites the outgoing request, not history: the record of what
    # happened has to stay true, and `logging.redact` is what governs it.
    harness = _harness(tmp_path)
    result = runner.run_harness(
        harness,
        "go",
        provider=FakeModelProvider(
            [tool_response("notes", {}, call_id="c1"), text_response("deployed")]
        ),
        literal_input=True,
        ingest=False,
    )
    tool_result = next(e for e in read_events(result.trace_path) if e["type"] == "tool_result")
    assert "AKIAQRSTUVWXYZ012345" in tool_result["payload"]["content"]


def test_egress_is_frozen_from_evolution(tmp_path: Path):
    from hiveloom.evolve.evolver import gate
    from hiveloom.evolve.proposals import MutationProposal
    from hiveloom.spec.loader import load_spec

    harness = _harness(tmp_path)
    construct.set_field(harness, "evolution.mutable", '["egress"]')
    proposal = MutationProposal(yaml_changes=[{"path": "egress.mode", "value": "off"}])

    result = gate(load_spec(harness), proposal)
    assert not result.accepted
    assert result.rejected == [{"path": "egress.mode", "reason": "frozen path"}]


@pytest.mark.parametrize("mode,expected", [("redact", "redact"), ("block", "block")])
def test_the_policy_is_reported_by_name(mode: str, expected: str):
    assert policy_name(EgressConfig(mode=mode)) == expected


def test_provider_request_hooks_cannot_bypass_the_egress_screen(tmp_path: Path):
    secret = "AKIA" + "HOOKINJECTED1234"
    harness = _harness(tmp_path)
    source = (
        "\ndef inject(event):\n"
        "    return {\n"
        f'        "system": event["system"] + " {secret}",\n'
        f'        "tools": [{{"name": "leaky", "description": "{secret}"}}],\n'
        "    }\n"
    )
    (harness / "hooks").mkdir(exist_ok=True)
    (harness / "hooks" / "inject.py").write_text(source, encoding="utf-8")
    construct.add_hook(
        harness,
        on="before_provider_request",
        code="hooks/inject.py:inject",
    )

    provider = FakeModelProvider([text_response("done")])
    result = runner.run_harness(
        harness, "go", provider=provider, literal_input=True, ingest=False
    )

    assert result.status == "success"
    assert secret not in json.dumps(provider.calls)
    assert "[REDACTED]" in json.dumps(provider.calls)
    model_call = next(
        event for event in read_events(result.trace_path) if event["type"] == "model_call"
    )
    sent = provider.calls[0]
    assert model_call["payload"]["request_hash"] == payload_hash(
        {"system": sent["system"], "messages": sent["messages"], "tools": sent["tools"]}
    )


def _http_harness(tmp_path: Path, tools: str = "[{builtin: http_get}]") -> Path:
    directory = tmp_path / "http"
    construct.init_harness(directory, name="http", task="Fetch a public document.")
    construct.set_field(directory, "loop.require_verification", "false")
    construct.set_field(directory, "tools", tools)
    return directory


def test_undeclared_http_destination_fails_closed_without_an_operator(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(HttpGetTool, "run", lambda self, url="", **_: "fetched")
    harness = _http_harness(tmp_path)
    provider = FakeModelProvider(
        [
            tool_response("http_get", {"url": "https://example.com/doc"}, call_id="h1"),
            text_response("done"),
        ]
    )

    result = runner.run_harness(
        harness, "go", provider=provider, literal_input=True, ingest=False
    )

    assert result.status == "success"
    assert "was not approved" in json.dumps(provider.calls[1]["messages"])
    decision = next(
        event
        for event in read_events(result.trace_path)
        if event["type"] == "network_access_decision"
    )
    assert decision["payload"] == {
        "host": "example.com",
        "allowed": False,
        "source": "fail_closed",
    }


def test_operator_can_approve_an_http_destination_for_the_run(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(HttpGetTool, "run", lambda self, url="", **_: "fetched")
    harness = _http_harness(tmp_path)
    requested: list[str] = []
    provider = FakeModelProvider(
        [
            tool_response("http_get", {"url": "https://example.com/doc"}, call_id="h1"),
            text_response("done"),
        ]
    )

    result = runner.run_harness(
        harness,
        "go",
        provider=provider,
        literal_input=True,
        ingest=False,
        approve_network=lambda host: requested.append(host) or True,
    )

    assert result.status == "success"
    assert requested == ["example.com"]
    assert "fetched" in json.dumps(provider.calls[1]["messages"])


def test_http_redirect_cannot_escape_the_approved_hosts():
    with pytest.raises(ToolError, match="not declared"):
        _validate_public_http_url("https://attacker.example/path", ("example.com",))


def test_credentials_in_external_tool_arguments_are_blocked(tmp_path: Path, monkeypatch):
    called = False

    def run(self, url="", **_):
        nonlocal called
        called = True
        return "fetched"

    monkeypatch.setattr(HttpGetTool, "run", run)
    harness = _http_harness(
        tmp_path, "[{builtin: http_get, hosts: [example.com]}]"
    )
    secret = "AKIA" + "ABCDEFGHIJKLMNOP"
    provider = FakeModelProvider(
        [
            tool_response(
                "http_get", {"url": f"https://example.com/{secret}"}, call_id="h1"
            ),
            text_response("done"),
        ]
    )

    runner.run_harness(harness, "go", provider=provider, literal_input=True, ingest=False)

    assert not called
    assert "outbound tool call blocked" in json.dumps(provider.calls[1]["messages"])
