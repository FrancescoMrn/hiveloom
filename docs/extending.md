# Extending hiveloom

hiveloom's catalog is open: everything a spec can reference — tools,
guardrails, validators, loop policies, compaction methods, event hooks, model
providers — is a **catalog entry**, and extensions register new entries through
one API. A registered entry shows up in `hiveloom catalog`, validates in specs
like a builtin, and appears in the generator's meta-prompt — so
`hiveloom generate` can weave harnesses with a capability the moment its pack
is installed.

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

`model.provider` is a registry name, not a hard-coded literal. Two ways to add
one:

**Declaratively** — `~/.hiveloom/models.yaml` for any OpenAI-compatible server
(Ollama, vLLM, LM Studio, Groq, OpenRouter…):

```yaml
providers:
  ollama:
    api: openai_compat
    base_url: http://localhost:11434/v1
    # api_key_env: OLLAMA_API_KEY   # optional
    models:
      - id: qwen3:8b
        input_cost_per_mtok: 0
        output_cost_per_mtok: 0
```

**Programmatically** — `hive.register_provider(name, factory, models=[...])`
with a factory returning a `ModelProvider`. Model pricing lives in the
registry; unknown models fall back to Haiku-class pricing so cost guardrails
stay conservative.

Generate/evolve can use any provider too: `--model ollama/qwen3:32b`
(`provider/model-id`). Note `model` stays in `ALWAYS_FROZEN` — the registry
widens what a human or generator may choose, never what evolution can mutate.

## Lifecycle event hooks

One event taxonomy serves the spec's `hooks:` section, ambient `hive.on(...)`
handlers, and the trace. Handlers get one dict payload; returning a dict
steers the run, returning `None` observes:

| Event | A returned dict may… |
|---|---|
| `context_assemble` | `{"messages": [...]}` — replace the message list |
| `before_tool_call` | `{"block": True, "reason": ...}` or `{"input": {...}}` |
| `after_tool_call` | `{"content": ...}` / `{"is_error": ...}` — patch the result |
| `before_verification` | `{"output": ...}` — replace final output before guardrails and validators |
| `before_compaction` | `{"cancel": True}` or `{"summary": "..."}` |
| `run_started` / `before_model_call` / `after_model_response` / `verification` / `run_finished` | observe only |

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
