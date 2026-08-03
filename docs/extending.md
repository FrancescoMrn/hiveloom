# Extending hiveloom

hiveloom's catalog is open: tools, guardrails, validators, loop policies,
compaction methods, event hooks, and model providers are **catalog entries**,
and extensions register new entries through one API. A registered entry shows
up in `hiveloom catalog`, validates in specs like a builtin, and appears in the
generator's meta-prompt — so `hiveloom generate` can weave harnesses with a
capability the moment its pack is installed. MCP tools are the dynamic
exception: they come from declared servers at run time and appear in
`hiveloom mcp list-tools`, not `hiveloom catalog`.

## Writing an extension

An extension is any module exposing `hiveloom_extension(hive)`:

```python
# my_ext.py
from hiveloom.ext import ExtensionAPI

def hiveloom_extension(hive: ExtensionAPI) -> None:
    # a plain function tool (schema derived from type hints)
    def ping() -> str:
        """Reply with pong."""
        return "pong"
    hive.register_function_tool(ping)

    # a configurable tool: factory(params, ctx) -> Tool; params come from the
    # spec entry `{builtin: slack_post, default_channel: "#ops"}`
    hive.register_tool(
        "slack_post", make_slack_tool,
        description="Post a message to a Slack channel.",
        tags=["network", "write"],
        params=[{"name": "default_channel", "type": "str"}],
    )

    hive.register_guardrail("pii_filter", make_pii_guardrail,
                            description="Block outputs containing PII.")
    hive.register_validator("json_diff", make_json_diff,
                            description="Diff output against a golden file.",
                            params=[{"name": "golden", "type": "str", "required": True}])
    hive.register_policy("reflexion", lambda p, c: ReflexionPolicy(),
                         description="One self-critique pass before finishing.")
    hive.register_compaction("keep_files", lambda p, c: KeepFilesCompaction(),
                             description="File-aware summarization.")
    hive.register_hook("audit_log", make_audit_handler,
                       description="Record every tool call to the audit sink.")
    hive.register_blueprint("scraper", "Always add no_network_write. Task: $ARGUMENTS")

    # ambient: runs for every harness in this process (e.g. org-wide audit)
    @hive.on("run_finished")
    def report(event):
        ...
```

Base classes to implement: `hiveloom.tools.registry.Tool`,
`hiveloom.guardrails.base.Guardrail`, `hiveloom.verify.base.Verifier`,
`hiveloom.loop.policies.LoopPolicy`,
`hiveloom.context.manager.CompactionMethod`.

## Where extensions load from

| Source | Scope | Failure behavior |
|---|---|---|
| pip packages with a `hiveloom.extensions` entry point (**packs**) | everywhere | collected, never crashes (`hiveloom extensions` shows errors) |
| `~/.hiveloom/extensions/*.py` | this user | collected, never crashes |
| `extensions:` list in `harness.yaml` (paths or module names) | that harness | `SpecError` — the harness can't run without it |

A **pack** declares its entry point in `pyproject.toml`:

```toml
[project.entry-points."hiveloom.extensions"]
acme-tools = "acme_tools:hiveloom_extension"
```

`hiveloom package` records the packs a spec's entries come from in
`hiveloom.lock` (`packs:` with name + version), and a run against a missing
pack fails with a message naming it. `hiveloom extensions` lists everything
loaded plus any load errors.

The `extensions:` spec path is **always frozen** — evolution can never add or
change the code a harness loads.

## Model providers

`model.provider` is a registry name, not a hard-coded literal. Builtin
providers already cover the major labs (`claude`, `openai`, `gemini`,
`mistral`, `deepseek`, `xai`), the aggregators (`groq`, `openrouter`,
`together`, `fireworks`), and local servers (`ollama`, `vllm`) — run
`hiveloom models` to list them. **[models.md](models.md) is the full
reference**: endpoints, key variables, pricing rules, open vs fixed catalogs,
and `~/.hiveloom/models.yaml` customisation.

What belongs here is the programmatic path, for anything that is not an
OpenAI-compatible HTTP endpoint:

```python
def setup(hive):
    hive.register_provider(
        "mylab",
        lambda ctx: MyProvider(),        # must return a ModelProvider
        api_key_env="MYLAB_API_KEY",
        base_url="https://api.mylab.example/v1",
        open_catalog=False,              # only the ids below validate
        models=[{"id": "mylab-small", "input_cost_per_mtok": 0.1,
                 "output_cost_per_mtok": 0.4}],
    )
```

Registering a name that already exists raises, so two packs cannot silently
fight over one provider name; only `models.yaml` may override a builtin.
Pricing lives in the registry, and unknown models fall back to conservative
Haiku-class pricing so cost guardrails never under-count.

Generate/evolve can use any provider too: `--model ollama/qwen3:32b`
(`provider/model-id`). Note `model` stays in `ALWAYS_FROZEN` — the registry
widens what a human or generator may choose, never what evolution can mutate.

## MCP servers

A harness can declare `mcp_servers`; their tools join the loop as ordinary
tools (`mcp__<server-name>__<tool>`), discovered eagerly when the tool
registry is built (including `run --dry-run`):

```bash
hiveloom add mcp-server --name search --stdio-command npx \
  --stdio-arg -y --stdio-arg @foo/mcp-search \
  --env-from-host API_KEY=FOO_SEARCH_API_KEY --dir ./h

hiveloom add mcp-server --name jira --url https://mcp.acme.com/mcp \
  --header-env 'Authorization=ACME_MCP_TOKEN' --tool search_issues --dir ./h

hiveloom mcp list-tools --dir ./h   # see what a declared server actually exposes
```

A stdio server (`--stdio-command`) is **arbitrary local exec** — the same
trust boundary as any other code hook. `mcp_servers` is **always frozen** from
evolution, the same risk class as `extensions`. A remote tool's own
`annotations` (e.g. `readOnlyHint`/`destructiveHint`) are **self-reported by
an untrusted server and are never a security boundary** — the real boundaries
are the always-frozen set plus harness trust gating.

## Lifecycle event hooks

One event taxonomy serves the spec's `hooks:` section, ambient `hive.on(...)`
handlers, and the trace. Handlers get one dict payload; returning a dict
steers the run, returning `None` observes:

| Event | A returned dict may… |
|---|---|
| `context_assemble` | `{"messages": [...]}` — replace the message list |
| `before_provider_request` | `{"system": ...}` / `{"messages": [...]}` / `{"tools": [...]}` — patch the outgoing request (this request only; runs after guardrails) |
| `before_tool_call` | `{"block": True, "reason": ...}` or `{"input": {...}}` |
| `after_tool_call` | `{"content": ...}` / `{"is_error": ...}` — patch the result |
| `before_verification` | `{"output": ...}` — replace final output before guardrails and validators |
| `before_compaction` | `{"cancel": True}` or `{"summary": "..."}` |
| `run_started` / `before_model_call` / `after_provider_response` / `after_model_response` / `verification` / `run_finished` | observe only |

```bash
hiveloom add hook --on before_tool_call --code hooks/audit.py:audit --dir ./h
hiveloom add hook --on before_verification --builtin strip_json_fence --dir ./h
```

(In `harness.yaml` the field is `event:` — unquoted `on:` is a YAML boolean.)
Handlers must not raise; one that does is logged as a `hook_error` trace event
and skipped. Guardrails remain the frozen safety layer and always run first;
hooks are the extensible middleware layer.

## Skills, deferred tools, tool ergonomics

**Skills** (progressive disclosure): `hiveloom add skill pdf-report
--description "..."` scaffolds `skills/pdf-report/SKILL.md` and lists it in the
spec. Only name + description enter the system prompt; the model reads the full
file on demand with `file_read`.

**Deferred tools**: mark a tool `deferred: true` and it stays out of the model's
payload until the auto-added `search_tools` tool activates it — keeps context
small for harnesses with many tools.

**Tool authors** get: `guidelines` (usage rules injected while the tool is
active), `prepare(kwargs)` (normalize model-mangled args before validation),
`run_with_updates(kwargs, on_update)` (+`supports_updates`) for streaming
progress as `tool_update` trace events, and `ToolResult(terminate=True)` to
end the run without a final model call (honored when every result in the batch
terminates).

## Blueprints (generator house style)

A blueprint is a markdown fragment appended to `hiveloom generate`'s
meta-prompt — preferred tools, guardrail posture, validator patterns for a
family of tasks. `$ARGUMENTS`/`$@` expand to the task, `$1..$9` to its words.

```bash
hiveloom generate "extract top HN stories" -o ./hn --blueprint scraper
```

Lookup order: `~/.hiveloom/blueprints/<name>.md`, then pack-registered
(`hive.register_blueprint`).

## Trust

Harness folders carry executable code. Folders **built on this machine**
(via `init`/construct/generate) are trusted automatically; a foreign folder
(unzipped artifact, clone) is gated before any of its code loads:

```bash
hiveloom trust ./foreign-harness      # or: hiveloom run ./foreign --approve
HIVELOOM_TRUST=always hiveloom run .  # CI; `never` refuses instead
```

Decisions live in `~/.hiveloom/trust.json`. Frozen paths protect a harness
from its evolution; trust protects a machine from a foreign harness.

## Embedding hiveloom

**Any language** — stream trace events as JSONL over stdout:

```bash
hiveloom run ./h --input notes.txt --stream
# {"type":"run_started",...}
# {"type":"tool_call",...}
# ...
# {"type":"run_result","ok":true,"status":"success",...}
```

**Python** — the semver-stable SDK:

```python
from hiveloom import run_harness, generate_harness, Hive

result = run_harness("./h", "notes.txt", on_event=lambda e: print(e.type))
```

`$HIVELOOM_HOME` relocates everything user-level (extensions, models.yaml,
blueprints, trust.json, the default Hive DB) — useful for tests and CI.
