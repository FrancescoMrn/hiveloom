# hiveloom

**Build durable agent harnesses so smaller models can perform repeatable,
verifiable tasks.**

A model is only one part of an agent. Tools, context, loop policy, guardrails,
and verification often decide whether the same model succeeds or fails.
hiveloom makes that surrounding system a self-contained folder that can be
validated, versioned, run anywhere, measured, and deliberately improved.

> **Status:** `0.3.1`. The spec, CLI, Python SDK, runtime, trace/Hive
> memory, generation, gated evolution, packaging, MCP integration, and HTTP
> serving surfaces are implemented.

## The moat: Your harness is the product

- **A durable artifact:** `harness.yaml` and its code hooks replace fragile,
  conversation-only setup.
- **One validated construction path:** CLI edits and model-generated plans use
  the same transactional API; invalid changes roll back.
- **Closed-loop evidence:** every run produces a version-hashed trace, so
  `stats` can show whether a harness change improved success, cost, or turns.
- **Safety outside the model:** cost limits, tool allowlists, redaction,
  verification, and frozen evolution fields are enforced in code.
- **Open and portable:** builtins, extension packs, custom providers, and MCP
  tools share one runtime contract; a harness remains a normal folder.

The *hive* is the collective memory of runs. The *loom* turns task intent and
that evidence into an improvable harness.

## Measured performance

The checked-in
[article-extractor benchmark](evals/article-extractor/RESULTS.md) evaluates 32
live URLs over three epochs (96 runs per arm). Raw and harness arms use the same
prompt and fetch tool; the difference is the harness scaffolding.

| Model / arm | Task success | Hallucination | Cost per success | p50 latency |
|---|---:|---:|---:|---:|
| Claude Haiku, raw | 2% | 98% | $0.3405 | 6.6s |
| Claude Haiku + hiveloom | **61%** | **11%** | **$0.0196** | 10.7s |
| Claude Sonnet, raw baseline | 99% | 0% | $0.0077 | 6.4s |
| Qwen 3 4B, raw / harness | 60% / **69%** | 18% / 17% | local | 3.3s / 5.9s |
| Qwen 3.6 35B, raw / harness | 78% / **83%** | 13% / **0%** | local | 15.4s / 18.2s |

On this task, the Haiku harness gained 59 percentage points over raw Haiku and
cut cost per successful result by about 17×. It did **not** beat raw Sonnet, and
the full results include a local-model arm where scaffolding reduced success.

Prompt caching (on by default for the `claude` provider) compounds this: in a
live measurement on a 7k-token harness prompt (Haiku 4.5, two-turn run), the
first run wrote the prefix to cache and every later run inside the cache TTL
read it back at a tenth of the input price — **$0.0019 per warm run vs $0.0100
cold, an 81% reduction**. A harness runs the same prompt shape every time,
which is exactly the workload prompt caching rewards.
That is the point of versioned evals: harness value is measured per task and
model, not assumed. See the
[methodology and caveats](evals/article-extractor/README.md).

## Install

```bash
uv pip install hiveloom
# or: pip install hiveloom
```

For development:

```bash
git clone <this-repository>
cd hiveloom-harness
uv sync --extra dev
```

## Five-minute quickstart

```bash
# Explore the contract without an API call
hiveloom schema --annotated
hiveloom catalog tools

# Construct a harness; every mutation validates and rolls back on error
hiveloom init ./summarizer --name summarizer \
  --task "Summarize a text file into JSON."
printf '%s\n' "The quick brown fox jumps over the lazy dog." \
  > ./summarizer/notes.txt
hiveloom add tool --builtin file_read --dir ./summarizer
hiveloom add validator --builtin regex_match --pattern '"summary"' \
  --dir ./summarizer
hiveloom validate ./summarizer --json

# Assemble the first call without contacting the model
hiveloom run ./summarizer --input notes.txt --dry-run --json

# Run for real (the default provider uses Anthropic)
export ANTHROPIC_API_KEY=sk-...
hiveloom run ./summarizer --input notes.txt --json

# …or any other lab. `hiveloom models` lists every provider and its key
# variable; OpenAI, Gemini, Mistral, DeepSeek, xAI, Groq, OpenRouter,
# Together, Fireworks, Ollama, and vLLM are builtin.
hiveloom models
hiveloom set model openai/gpt-4.1-mini --dir ./summarizer

# Inspect evidence and propose a gated improvement after failures
hiveloom stats ./summarizer --json
hiveloom evolve ./summarizer --propose --json
```

Prefer model-driven construction?

```bash
hiveloom generate "Summarize a text file into JSON." -o ./summarizer --json
```

A complete credential-free example is in
[`harnesses/example-summarizer`](harnesses/example-summarizer).

## A harness is a folder

```text
my-harness/
├── harness.yaml          # declarative runtime contract
├── tools/                # optional code tools
├── validators/           # optional task-specific verification
├── schemas/
├── skills/
├── .hiveloom/traces/     # append-only run memory
├── .env.example
└── requirements.txt
```

Do not hand-edit `harness.yaml`; use `init`, `add`, `set`, `remove`, or the
gated `evolve` flow.

## Core interfaces

Every CLI command supports `--json`. Exit codes are stable:
`0` success, `1` verification failed, `2` guardrail halt, `3` invalid spec or
request, and `4` runtime failure.

```bash
# Explore and construct
hiveloom schema --json
hiveloom explain context.compaction --json
hiveloom catalog validators --json
hiveloom set loop.max_turns 20 --dir ./my-harness --json
hiveloom add tool --builtin file_read --dir ./my-harness --json

# Run and inspect
hiveloom run ./my-harness --input input.txt --stream
hiveloom trace <run-id> --json
hiveloom stats ./my-harness --json

# Improve with a human gate
hiveloom evolve ./my-harness --propose --json
hiveloom proposals list ./my-harness --json
hiveloom proposals apply ./my-harness <proposal-id> --json

# Extend and ship
hiveloom extensions --json
hiveloom mcp list-tools --dir ./my-harness --json
hiveloom package ./my-harness --docker --json
hiveloom serve ./my-harness
hiveloom mcp serve ./my-harness ./other-harness
```

`run --dry-run` makes no model call, but declared MCP servers are contacted
because their tools are discovered eagerly. `serve` provides `/healthz` and
`/runs`; the separately documented `control-plane` is a scoped,
bearer-authorized, non-production operational API.

`mcp serve` is the agent-facing front door: it exposes each harness as an MCP
tool (`run_<name>`) plus a `list_harnesses` tool that carries each harness's
measured success rate and cost, so any MCP-capable agent can pick a harness on
evidence and delegate a task to it — getting back a structured,
validator-checked result instead of improvising the task itself. Input is
always treated as literal text, and untrusted directories fail at startup
(approve them with `hiveloom trust`). Register harnesses once with
`hiveloom registry add <dir>` and serve them all with `--registered`; add
`--http` (with `HIVELOOM_API_KEY`) to serve over streamable HTTP instead of
stdio.

### Python SDK

The root package exposes the small semver-stable embedding surface:

```python
from hiveloom import (
    Hive,
    dry_run,
    generate_harness,
    load_spec,
    run_harness,
    validate_harness,
)

info = dry_run("./my-harness", "input.txt")
result = run_harness(
    "./my-harness",
    "input.txt",
    on_event=lambda event: print(event.type),
)
spec = generate_harness("Reconcile invoices", "./invoice-reconciler")
```

Inject a `ModelProvider` into `run_harness` or a `StrongModel` into
`generate_harness` for custom embedding and deterministic tests. For
language-neutral integration, use `run --stream` (JSONL) or `serve` (HTTP).

## Safety invariants

- Evolution cannot change `guardrails`, `model`, `logging.redact`,
  `extensions`, `hooks`, `mcp_servers`, or `evolution.auto_propose`.
- The cost guardrail defaults on at `$1.00`.
- The shell tool is disabled unless explicitly configured and remains
  allowlist-only.
- Redaction runs before trace persistence.
- Foreign harness code is trust-gated before loading.

## Documentation

- [Agent entry point](AGENTS.md) and [lifecycle skills](skills/README.md)
- [Harness spec](docs/spec.md)
- [Architecture](docs/architecture.md)
- [Models and providers](docs/models.md)
- [Extensions](docs/extending.md)
- [Deployment and evolution](docs/deploying-and-evolving.md)
- [Control plane](docs/control-plane.md)
- [Contributing and QA](CONTRIBUTING.md)
- [Security policy](SECURITY.md)

Agent guidance ships in the wheel: `hiveloom guide --list`.

Apache-2.0 licensed.
