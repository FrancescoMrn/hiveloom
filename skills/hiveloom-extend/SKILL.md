---
name: hiveloom-extend
description: >-
  Extend hiveloom's open catalog with new tools, guardrails, validators, loop
  policies, compaction methods, event hooks, model providers, and blueprints.
  Use when a harness needs a capability the builtin catalog lacks, when adding
  a custom/local LLM provider (Ollama, vLLM, OpenAI-compatible), or when
  writing a reusable extension pack. Triggers: "add a custom tool to
  hiveloom", "use ollama/vLLM with hiveloom", "hiveloom extension pack".
---

# Extending hiveloom

Builtin and extension-registered capabilities are **catalog entries**, and the
catalog is open. A registered entry validates in specs, lists in
`hiveloom catalog`, and flows into the generator meta-prompt — install a pack
and `hiveloom generate` can immediately use it. MCP tools are discovered
dynamically and list under `hiveloom mcp list-tools` instead. Check what's
loaded (and any load errors) with:

```bash
hiveloom extensions
```

## Decide the scope first

| Need | Mechanism |
|---|---|
| One harness, task-specific logic | code hook in the harness (`hiveloom add tool --code …`) — see `hiveloom-build` |
| Everything on this machine/user | `~/.hiveloom/extensions/*.py` |
| Reusable/shareable across machines | a **pack**: pip package with a `hiveloom.extensions` entry point |
| One harness, shared module | `extensions:` list in `harness.yaml` (loaded before validation; always frozen) |

## Writing an extension

Any module exposing `hiveloom_extension(hive)`:

```python
from hiveloom.ext import ExtensionAPI

def hiveloom_extension(hive: ExtensionAPI) -> None:
    def ping() -> str:
        """Reply with pong."""
        return "pong"
    hive.register_function_tool(ping)          # schema derived from type hints

    hive.register_tool("slack_post", make_slack_tool,   # factory(params, ctx) -> Tool
        description="Post a message to a Slack channel.",
        tags=["network", "write"],
        params=[{"name": "default_channel", "type": "str"}])
    hive.register_guardrail("pii_filter", make_pii_guardrail, description="...")
    hive.register_validator("json_diff", make_json_diff, description="...",
        params=[{"name": "golden", "type": "str", "required": True}])
    hive.register_policy("reflexion", lambda p, c: ReflexionPolicy(), description="...")
    hive.register_compaction("keep_files", lambda p, c: KeepFilesCompaction(), description="...")
    hive.register_hook("audit_log", make_audit_handler, description="...")
    hive.register_blueprint("scraper", "Always add no_network_write. Task: $ARGUMENTS")

    @hive.on("run_finished")        # ambient: every harness in this process
    def report(event): ...
```

Base classes: `hiveloom.tools.registry.Tool`, `hiveloom.guardrails.base.Guardrail`,
`hiveloom.verify.base.Verifier`, `hiveloom.loop.policies.LoopPolicy`,
`hiveloom.context.manager.CompactionMethod`.

A pack declares its entry point in `pyproject.toml`:

```toml
[project.entry-points."hiveloom.extensions"]
acme-tools = "acme_tools:hiveloom_extension"
```

## Model providers (other labs, local/custom LLMs)

The major labs are **builtin** — no configuration, just the key. Run
`hiveloom models` (add `--json`) for names, key variables, endpoints, and
pricing: `claude`, `openai`, `gemini`, `mistral`, `deepseek`, `xai`, `groq`,
`openrouter`, `together`, `fireworks`, `ollama`, `vllm`.

Before a model matrix, inspect declarations with `hiveloom models probe ./h
--provider PROVIDER --model MODEL --json`. Add `--live --identity exact` only
when you intend to make up to two possibly billed calls. Use repeated
`--alias` values with `--identity alias` for documented provider aliases;
never hide the effective model.

Switch a harness to another lab with the **`provider/model-id` selector**:

```bash
hiveloom set model openai/gpt-4.1-mini --dir ./h
hiveloom generate ... --model ollama/qwen3:32b
```

Use `set model`, never `set model.provider` / `set model.id` separately —
those two fields validate against each other, so any one-at-a-time edit is
rolled back. The selector splits on the first `/` only, so aggregator ids keep
their own slashes (`openrouter/deepseek/deepseek-r1`).

Every provider except `claude` has an **open catalog**: ids released after this
hiveloom version validate fine. `claude` is fixed, so a typo there is caught.

To add a server hiveloom does not ship, or to correct pricing, use
`~/.hiveloom/models.yaml` — omit `base_url` to extend a builtin, supply it to
override one or declare a new provider:

```yaml
providers:
  lmstudio:
    api: openai_compat
    base_url: http://localhost:1234/v1
    models:
      - id: qwen3:8b
        input_cost_per_mtok: 0
        output_cost_per_mtok: 0
```

Programmatically: `hive.register_provider(name, factory, models=[...])`.
Model entries may declare `supports_tool_calling`,
`supports_structured_output`, and `supports_reasoning_replay`. A custom adapter
may override `probe_capabilities()` when the generic tool/reasoning probe is
not enough.

Unknown hosted models fall back to Haiku-class pricing so cost guardrails stay
conservative; unknown local ones are free. Reasoning-style models (DeepSeek-R1
family etc.) are supported. Full reference: `docs/models.md`.

## Event hooks (middleware, not guardrails)

Attach with `hiveloom add hook --on <event> --code|--builtin …`. Handlers get
one dict payload; returning a dict steers (`before_tool_call` →
`{"block": True}` or `{"input": {...}}`; `after_tool_call` → patch the result;
`before_verification` → replace the output; `context_assemble` → replace
messages; `before_compaction` → cancel or supply the summary;
`before_provider_request` → patch the outgoing `system`/`messages`/`tools`
for that one request, after guardrails). Returning `None` observes —
`after_provider_response` observes per-call `usage`/`cost_usd`. In `harness.yaml` the field is `event:` — unquoted `on:` is a
YAML boolean. Handlers must not raise; a raising handler is logged as
`hook_error` and skipped. Guardrails remain the frozen safety layer and always
run first.

## MCP servers

`mcp_servers` tools join the loop as ordinary tools (`mcp__<name>__<tool>`),
discovered eagerly (including on `run --dry-run`):

```bash
hiveloom add mcp-server --name search --stdio-command npx \
  --stdio-arg -y --stdio-arg @foo/mcp-search --dir ./h
hiveloom mcp list-tools --dir ./h   # see what it actually exposes
```

A stdio server is arbitrary local exec — trust-gated like any code hook.

## Rules that never bend

Extensions **widen choice, never the evolution gate**: `guardrails`, `model`,
`logging.redact`, `extensions`, `hooks`, `mcp_servers`, and
`evolution.auto_propose` and `evolution.trace_excerpts` stay frozen from
evolution, and foreign harness
folders stay trust-gated before their code loads.

Full reference (deferred tools, tool ergonomics, `$HIVELOOM_HOME`, SDK):
`docs/extending.md`.
