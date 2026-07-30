# hiveloom

**Generate, run, and evolve agent harnesses on the fly — so small, cheap models reliably perform company-relevant tasks.**

Performance of agentic systems is governed as much by the **harness** (tools, loop policy, context strategy, guardrails, verification) as by the model itself. hiveloom's bet:

1. A strong model (or a human) **generates** a harness for a specific task.
2. A small, cheap model **executes** inside that harness.
3. Execution traces are treated as **memory**, feeding an **evolution loop** that improves the harness after failures.

The harness — not the conversation — is the durable, versionable, improvable artifact. The *hive* is the collective memory of runs; the *loom* weaves harnesses from that memory.

> **Status:** 0.2.0 beta. The core library and CLI are implemented: the harness spec + loader, the explore/construct commands, the runtime (`run`), the Hive (`trace`/`stats`), generation + evolution (`generate`/`evolve`), and packaging (`package`). See the [documentation](#documentation) for architecture, extension, and deployment details.

## Install

```bash
uv pip install -e ".[dev]"     # or: pip install -e ".[dev]"
```

## Quickstart (5 minutes)

Build, run, and inspect a harness that summarizes a text file into structured JSON.

```bash
# 1. Learn the contract (read-only, no API key needed)
hiveloom schema --annotated
hiveloom catalog tools

# 2. Build a harness step by step (each step is validated + rolled back on error)
hiveloom init ./summarizer --name summarizer --task "Summarize a text file into JSON."
printf '%s\n' "The quick brown fox jumps over the lazy dog." > ./summarizer/notes.txt
hiveloom add tool --builtin file_read --dir ./summarizer
hiveloom add validator --builtin regex_match --pattern '"summary"' --dir ./summarizer
hiveloom validate ./summarizer

# 3. Dry-run to see the first model call assembled — still no API use
hiveloom run ./summarizer --input notes.txt --dry-run

# 4. Run for real (needs ANTHROPIC_API_KEY; the small executor model runs inside)
echo "ANTHROPIC_API_KEY=sk-..." > ./summarizer/.env
hiveloom run ./summarizer --input notes.txt --json

# 5. Inspect memory, then improve after failures
hiveloom stats ./summarizer          # success rate / cost / turns per version hash
hiveloom evolve ./summarizer         # analyze Hive failures → propose a gated mutation
```

Prefer to let a strong model build it? `hiveloom generate "Summarize a text file into JSON." -o ./summarizer`.
A complete, tested example lives in [`harnesses/example-summarizer/`](harnesses/example-summarizer).

## The harness is a folder

A harness is a self-contained, portable directory:

```
<harness-name>/
├── harness.yaml          # the declarative spec (YAML + code hooks)
├── tools/  validators/  schemas/
├── .hiveloom/traces/     # in-folder trace dir — memory travels with the harness
├── .env.example
├── requirements.txt
└── README.md
```

## CLI (agent-native)

The CLI is designed to be driven by an agent. Every command has an agent-oriented
`--help`, supports `--json`, and every mutating command validates the full spec and
rolls back on error — a harness dir is never left invalid.

**Explore the contract**

```bash
hiveloom schema --annotated        # commented YAML template (a valid spec)
hiveloom schema --json             # JSON schema
hiveloom catalog tools             # tools|guardrails|validators|policies|compaction|hooks
hiveloom explain context.compaction
hiveloom validate <harness_dir>
hiveloom extensions                # loaded packs/extensions/providers + load errors
```

**Construct incrementally** (each step validated + rolled back on error)

```bash
hiveloom init ./my-harness --name my-harness --task "Summarize a text file."
hiveloom set loop.max_turns 30 --dir ./my-harness
hiveloom set system_prompt --file prompt.txt --dir ./my-harness
hiveloom add tool --builtin file_read --dir ./my-harness
hiveloom add tool --code tools/fetch.py:fetch --description "..." --dir ./my-harness
hiveloom add validator --code validators/check.py:validate --dir ./my-harness
hiveloom add guardrail --builtin max_cost_usd --value 0.50 --dir ./my-harness
hiveloom add hook --on before_tool_call --code hooks/audit.py:audit --dir ./my-harness
hiveloom add skill pdf-report --description "Build a PDF report." --dir ./my-harness
hiveloom remove file_read --dir ./my-harness
```

**Run**

```bash
hiveloom run ./my-harness --input notes.txt          # runs the agent loop
hiveloom run ./my-harness --input notes.txt --dry-run # assemble first call, no API use
hiveloom run ./my-harness --input notes.txt --json    # machine-readable result
hiveloom run ./my-harness --input notes.txt --stream  # trace events as JSONL (embedding)
```

`run` needs `ANTHROPIC_API_KEY` (loaded from the harness `.env` if present). Traces are
written to the harness's `.hiveloom/traces/<run_id>.jsonl`. The small executor model
(default `claude-haiku-4-5`) runs inside the harness; guardrails and verification gate it.

**Inspect memory**

```bash
hiveloom trace <run_id>            # a run's summary + ordered trace events
hiveloom stats ./my-harness        # success rate / cost / turns per version hash
hiveloom stats my-harness-name     # by harness name (from the Hive)
```

Every `run` ingests its trace into the Hive (`~/.hiveloom/hive.db`, override with
`$HIVELOOM_DB`), idempotently by `run_id`. `stats` also ingests a harness dir's in-folder
traces on the fly — so a harness that ran in production for a week can be copied back and
its real failures analyzed and evolved against. Version-hash bucketing is what proves a
harness mutation actually helped.

**Generate & evolve**

```bash
hiveloom generate "Reconcile invoices against PO numbers" -o ./recon   # strong model builds it
hiveloom evolve ./recon            # analyze Hive failures, propose a gated mutation
hiveloom evolve ./recon --yes      # auto-apply YAML changes (code always needs y/n)
hiveloom evolve ./recon --propose  # queue the gated proposal instead of applying it
hiveloom proposals list ./recon    # review, then `proposals apply` / `proposals reject`
```

`generate` is sugar: a strong model produces a construction *plan* that hiveloom replays
through the same validated `init`/`add`/`set` functions (with a validate/repair loop), so
there is one code path for building harnesses. `evolve` reads the Hive's clustered failures,
asks a strong model for a minimal mutation, and **gates it in code**: `guardrails`, `model`,
and `logging.redact` can never be changed, changes must fall within the harness's `mutable`
set, and regenerated code hooks always require explicit approval. Applied mutations bump an
`# evolved: N` counter and are recorded in the Hive under a new version hash. Both need
`ANTHROPIC_API_KEY`.

**The deploy-anywhere-keep-evolving loop:** a harness runs wherever you put it (cheap model),
writes traces in-folder, and is evolved deliberately on a dev/CI box (strong model + human
gate) against the traces you collect back. See
[`docs/deploying-and-evolving.md`](docs/deploying-and-evolving.md) for the full loop,
deployment topologies, and what's intentionally left to your own tooling.

Exit codes: `0` ok, `1` verify failed, `2` guardrail halt, `3` spec/validation error, `4` runtime error.

## Extending (the open catalog)

Everything a spec references is a **catalog entry**, and the catalog is open:
extensions register new tools, guardrails, validators, loop policies,
compaction methods, event hooks, and model providers through one API
(`hiveloom.ext.ExtensionAPI`). Registered entries validate in specs, list in
`hiveloom catalog`, and flow into the generator meta-prompt — install a pack
and `hiveloom generate` can immediately weave harnesses with it.

```bash
hiveloom extensions                                   # what's loaded, from where
hiveloom generate "task" -o ./h --blueprint scraper   # house-style prompt fragments
hiveloom run ./h --input x --stream                   # embed via JSONL event stream
hiveloom trust ./foreign-harness                      # gate foreign folders' code
```

Extensions load from pip packages (`hiveloom.extensions` entry point — a
*pack*), `~/.hiveloom/extensions/*.py`, and a harness's own `extensions:` list.
Custom LLM providers (Ollama, vLLM, any OpenAI-compatible server) are one
`~/.hiveloom/models.yaml` entry. See [`docs/extending.md`](docs/extending.md).

## Documentation

**For agents:** [AGENTS.md](AGENTS.md) is the entry point — ground rules, exit
codes, and a task→skill map. [skills/](skills/README.md) holds a series of
installable Agent Skills (build / run / evolve / extend / ship); the root
[SKILL.md](SKILL.md) is the compact all-in-one variant.

- [Architecture](docs/architecture.md) — components, data flow, and invariants.
- [Harness spec reference](docs/spec.md) — the declarative contract and builtins.
- [Extending hiveloom](docs/extending.md) — extension packs, providers, hooks, and SDK embedding.
- [Deploying and evolving](docs/deploying-and-evolving.md) — portable artifacts and the production feedback loop.
- [Control plane](docs/control-plane.md) — ed25519 keys and bearer-token auth (stub; the HTTP server follows in a later task).
- [Examples](harnesses/) — summarization, market analysis, and HN extraction harnesses.

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development workflow and
[SECURITY.md](SECURITY.md) for responsible vulnerability reporting and the
harness trust model.

## Safety invariants

* The evolver may never modify `guardrails`, `model`, `logging.redact`, or `extensions`.
* The cost guardrail defaults **on** (`max_cost_usd: 1.00`) even if omitted.
* `shell` is allowlist-only and disabled unless the spec enables it.
* Redaction patterns are applied before any trace is persisted.
* Foreign harness folders are trust-gated before any of their code loads
  (`hiveloom trust`, `--approve`, or `HIVELOOM_TRUST` in CI).

## License

Apache-2.0.
