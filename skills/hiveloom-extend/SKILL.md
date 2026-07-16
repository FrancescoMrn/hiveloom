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

Everything a spec references is a **catalog entry**, and the catalog is open.
A registered entry validates in specs, lists in `hiveloom catalog`, and flows
into the generator meta-prompt — install a pack and `hiveloom generate` can
immediately use it. Check what's loaded (and any load errors) with:

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

## Model providers (local/custom LLMs)

Declaratively, for any OpenAI-compatible server — one entry in
`~/.hiveloom/models.yaml`:

```yaml
providers:
  ollama:
    api: openai_compat
    base_url: http://localhost:11434/v1
    models:
      - id: qwen3:8b
        input_cost_per_mtok: 0
        output_cost_per_mtok: 0
```

Then `hiveloom set model.provider ollama` / `set model.id qwen3:8b`, or
`hiveloom generate ... --model ollama/qwen3:32b`. Programmatically:
`hive.register_provider(name, factory, models=[...])`. Unknown models fall
back to Haiku-class pricing so cost guardrails stay conservative.

## Event hooks (middleware, not guardrails)

Attach with `hiveloom add hook --on <event> --code|--builtin …`. Handlers get
one dict payload; returning a dict steers (`before_tool_call` →
`{"block": True}` or `{"input": {...}}`; `after_tool_call` → patch the result;
`before_verification` → replace the output; `context_assemble` → replace
messages; `before_compaction` → cancel or supply the summary). Returning
`None` observes. In `harness.yaml` the field is `event:` — unquoted `on:` is a
YAML boolean. Handlers must not raise; a raising handler is logged as
`hook_error` and skipped. Guardrails remain the frozen safety layer and always
run first.

## Rules that never bend

Extensions **widen choice, never the evolution gate**: `model`, `guardrails`,
`logging.redact`, and `extensions` stay frozen from evolution, and foreign
harness folders stay trust-gated before their code loads.

Full reference (deferred tools, tool ergonomics, `$HIVELOOM_HOME`, SDK):
`docs/extending.md`.
