# hiveloom for agents

hiveloom turns a task into a **harness**: a self-contained folder
(`harness.yaml` + code hooks) that scaffolds tools, loop policy, context
strategy, guardrails, and verification around a small executor model. The CLI
is agent-native: every command has `--json`, every mutating command validates
the whole spec and rolls back on error, and the contract is machine-emittable
(`hiveloom schema --json`). This file is the entry point for an agent driving
the library; humans should start at [README.md](README.md).

## Ground rules

1. **Never hand-edit `harness.yaml`.** Construct via `init`/`add`/`set`/
   `remove`; improve via `evolve`. Both validate and roll back — hand edits
   bypass that.
2. **Always pass `--json`** and branch on the result and the exit code:
   `0` success · `1` verify failed · `2` guardrail halt · `3` spec/validation
   error · `4` runtime error.
3. **The catalog is the truth.** If `hiveloom catalog <kind>` doesn't list it,
   it doesn't exist. Check `hiveloom extensions` — installed packs may have
   widened the catalog.
4. **Never weaken the safety layer**: `guardrails`, `model`, `logging.redact`,
   and `extensions` are frozen from evolution; the cost guardrail defaults on;
   `shell` is allowlist-only; foreign harness folders are trust-gated before
   their code loads. Don't route around any of this on a user's behalf.
5. **Free exploration is free.** `schema`, `catalog`, `explain`, `validate`,
   `extensions`, and `run --dry-run` never touch the API. `run`, `generate`,
   and `evolve` need `ANTHROPIC_API_KEY`.

## Task → skill map

Focused skills live in [`skills/`](skills/README.md); the compact all-in-one
variant is the root [`SKILL.md`](SKILL.md).

| You are asked to… | Load | Core commands |
|---|---|---|
| Create a harness for a task | [`skills/hiveloom-build`](skills/hiveloom-build/SKILL.md) | `schema --annotated`, `catalog`, `init`, `add`, `set`, `validate`, `run --dry-run`, `generate` |
| Run one / debug a run / check stats | [`skills/hiveloom-run`](skills/hiveloom-run/SKILL.md) | `run [--json\|--stream\|--dry-run]`, `trace`, `stats` |
| Improve a failing harness | [`skills/hiveloom-evolve`](skills/hiveloom-evolve/SKILL.md) | `evolve [--yes\|--propose]`, `proposals list\|show\|apply\|reject`, `stats` |
| Add capabilities / custom LLM provider | [`skills/hiveloom-extend`](skills/hiveloom-extend/SKILL.md) | `extensions`, `ExtensionAPI`, `~/.hiveloom/models.yaml` |
| Ship / receive / deploy-and-evolve loop | [`skills/hiveloom-ship`](skills/hiveloom-ship/SKILL.md) | `package [--docker]`, `trust`, `stats` |

A harness with `evolution.auto_propose.enabled: true` may already have queued a
`trigger=auto` proposal after a failing `run` — check `proposals list` before
assuming you need to run `evolve --propose` yourself. It only ever drafts;
applying still needs an explicit `proposals apply`.

## Reference docs

- [docs/spec.md](docs/spec.md) — the spec contract and builtins (the live
  source is `hiveloom schema`/`explain`).
- [docs/architecture.md](docs/architecture.md) — components, data flow,
  design invariants.
- [docs/extending.md](docs/extending.md) — extension packs, providers, hooks,
  SDK embedding.
- [docs/deploying-and-evolving.md](docs/deploying-and-evolving.md) — portable
  artifacts and the production feedback loop.
- [harnesses/](harnesses/) — working example harnesses to imitate.

## Working on hiveloom itself

Dev setup, test, and lint workflow: [CONTRIBUTING.md](CONTRIBUTING.md)
(`uv sync --extra dev`, `uv run pytest`, `uv run ruff check .`). Tests must not
need credentials or network — use the fake provider and `$HIVELOOM_HOME`
isolation as the existing tests do.
