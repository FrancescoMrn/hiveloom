# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Provider responses can report the served model, provider request ID, billed
  amount and currency, a USD conversion, opaque reasoning replay data, and
  bounded JSON metadata. Each call exposes whether Hiveloom used a billed or
  estimated USD cost, and the public run result keeps a receipt for every
  provider call.
- `normalize_openai_response`, `to_openai_messages`, and `to_openai_tool` are
  supported public codecs for OpenAI-compatible extensions. The old private
  names remain aliases for compatibility.

## [1.0.0] - 2026-08-28

The observability release. A run now leaves behind a progressive,
tamper-evident **journal** you can read back, verify, and **fork** from any
model call in it — replaying the exact prefix against an edited harness, or
against a different model. The **workbench** (`devtools/ui`) is the graphical
front end for all of it. A run is now the only execution identity: sessions are
gone.

### Changed

- **Forks live inside the harness they came from.** `hiveloom fork` now writes
  to `<harness>/.hiveloom/forks/<name>` by default, where the workbench has
  always put them; the two no longer disagree. `--name` picks the folder
  (slug-checked — a name, not a path); `--dir` still opts a one-off out of
  containment for a developer's own shell, but the workbench has no such
  escape hatch. A fork is an experiment *on* a harness rather than a harness of
  its own, so archiving or packaging the folder takes its experiments with it,
  a directory of harnesses stays a directory of harnesses, and the parent's
  file tools cannot reach a running experiment. Forking a fork now produces a
  sibling under the same original harness instead of nesting a level deeper:
  depth would record generation, and `fork.yaml` already does.
  `fork.fork_target()` is the one resolver the CLI, workbench and MCP share.
- **The workbench nests forks under the harness that contains them.** Catalog
  rows carry `root_path`, `is_fork` and `parent_id`, and the rail groups by
  containment rather than by harness name — so a renamed fork stays with its
  harness, and two unrelated harnesses that share a name stay two harnesses.
- **The demo harnesses were rebuilt from zero, and there are now five of
  them.** Each was constructed through the same validated `init`/`add`/`set`
  path the CLI exposes — nothing hand-writes `harness.yaml` — and committed
  as a plain folder. The set is curated to one harness per layer: `quickstart`
  (no tools), `example-summarizer` (builtin tools + schema and code
  verification), `article-extractor` (a custom `@tool`, an output hook, a
  validator that re-fetches the page), `ticket-triage` (an MCP server, FastMCP
  over stdio, as the harness's only data source), and `routing-lab` (playbooks
  that move the model *and*
  the tool set mid-run, on an offline provider, and shipping a real fork under
  `.hiveloom/forks/`). `hello`, `hn-extractor` and `company-analyst` were
  folded into those four and removed, along with two stray fork folders that
  had been sitting beside their harness rather than inside it.

- **A harness declares its dependencies in `pyproject.toml`.** `hiveloom init`
  now scaffolds a PEP 621 `[project]` table pinning the runtime that built it
  (`hiveloom==1.0.0`) instead of a `requirements.txt` no resolver treats as
  authoritative — `uv sync` inside the folder installs it. The folder is still
  not a package: `[tool.uv] package = false` says so, nothing is built, and
  `.venv/` is ignored by the scaffolded `.gitignore` and excluded from
  `hiveloom package` output the way `.hiveloom/` and `.env` already were. The
  four demo harnesses carry the new file.

### Removed

- **Sessions.** A run is now the only execution identity across the CLI, API,
  Hive, journal, and workbench. `session_id`, grouped trace directories,
  `Hive.sessions()` / `session_runs()`, and `/api/sessions` were removed rather
  than retained as aliases. The workbench hierarchy is harness → version →
  run, and branching always creates a derived run.

### Added

- **The workbench ships on npm as `hiveloom-workbench`.** It is fetched on
  purpose and never by default: the `hiveloom` wheel stays what runs a harness
  in production, and nobody deploying one carries a web interface they will
  never open.

  ```sh
  uv add hiveloom          # the framework
  npx hiveloom-workbench   # the inspector, when you want it
  ```

  - **One package, three parts**: the compiled interface, the Python API
    (`server.py` and the bundled copilot harness), and a Node launcher. Because
    they ship as a single artifact, a UI that disagrees with its API about any
    of the 45 routes is impossible by construction.
  - The launcher finds the interpreter that already has hiveloom — an explicit
    `--python`, `$VIRTUAL_ENV`, your project's `uv` environment, or one `uv`
    creates on demand — starts the API on a private loopback port, and serves
    the interface in front of it, proxying `/api`. One origin, so no CORS and no
    base URL to configure, and the API is unreachable from anywhere else.
    Streaming is piped rather than buffered, so a live run's turns still arrive
    as they happen.
  - `hiveloom ui` is a wrapper around the same `npx` command, and says what to
    install when Node is missing rather than failing as a crash.
  - An installed workbench keeps its state under `~/.hiveloom/workbench/` —
    conversations, memory, and a writable copy of the bundled copilot, which
    journals its runs and so cannot live inside `node_modules`. An upgrade
    refreshes what shipped and leaves the journals and `.env` alone. A checkout
    still keeps everything in `devtools/ui/.hiveloom/`, so development never
    writes into the directory an install uses.
  - New `GET /api/health` reports service, version, and whether a bundle is
    present, which is what the launcher hand-shakes against before opening a
    browser.

- **The run journal** — every run's JSONL record is now progressive,
  self-describing, and tamper-evident.
  - The conversation is recorded **once**, message by message, as
    `context_append` events; the system prompt and tool payload are recorded
    only when they change (`context_system`, `context_tools`). A `model_call`
    now references the folded context (`context_head`, `system_hash`,
    `tools_hash`, `messages_hash`) instead of re-snapshotting the whole
    request every turn. On a 12-call run this cut the trace from 676 KiB to
    184 KiB, and `model_call` from 85% of the file to 2.5%.
  - `hiveloom.logging.journal` is the fold that reads it back — `state_at`,
    `state_at_model_call`, `fold_events` — shared by every reader.
    `hiveloom trace --materialize <seq>` prints the exact `(system, messages,
    tools)` triple a call was built from, and warns when a `context_assemble`
    hook patched the request without persisting it.
  - Events are **chained**: each carries `prev`, the sha256 of the preceding
    written line. `hiveloom trace --verify` reports an edited or removed line
    with the position it broke at, and exits `4`. Truncating the tail is not a
    break — a prefix of an append-only log is always valid. Pre-1.0 traces are
    reported as `unchained` rather than broken.
  - `run_started` carries a **harness snapshot**: the dumped spec plus a
    `path -> sha256` manifest of every local behavioural file (the same set the
    version hash fingerprints). `logging.snapshot_files: true` inlines the
    bodies too, bounded at 256 KiB, reporting what it skipped.
  - `run_finished` now carries the run's `output`, `verdicts`, and `artifacts`.
- **Fork a run** — `hiveloom fork <run_id> --at <seq>` re-enters a finished run
  at one of its model calls, so a failure can be replayed from the turn it went
  wrong on instead of re-run from scratch.
  - Fork points are model calls, because the folded state immediately before
    one is by construction a valid provider request; an arbitrary seq can land
    mid-turn with a dangling `tool_use` and no result. `--list` shows them, and
    a seq that names something else snaps back to the previous call and says so.
  - The fork directory holds the harness that *actually ran* — spec rebuilt
    from the journal snapshot, files verified against its `sha256` manifest —
    plus `fork.yaml` (lineage, pinned to the sha256 of the exact journal line)
    and the folded conversation. A file that changed since the parent run
    refuses the fork; `--allow-drift` overrides and warns.
  - `hiveloom run <dir> --resume` replays that prefix verbatim against the
    fork's (edited) harness. `runner.run_harness(resume_messages=..., lineage=...)`
    is the library seam.
  - Forking is refused outright when the parent's hash chain is broken, or when
    the parent predates 1.0 and carries no harness snapshot. A prefix
    containing `[REDACTED]` spans warns: redaction happens before persistence,
    so the fork sends the marker where the parent sent a value.
- **Mid-run model hot-swap** — the executing model can change while a run is in
  flight, through two surfaces and no others.
  - **Declarative**: a playbook may declare `model:` (and `model_provider:`).
    Profile on a cheap model, decide on an expensive one, in one harness and
    one conversation. Leaving the mode restores the harness default, so a mode
    is a configuration and not a one-way door. Both fields are **frozen from
    evolution**, joining top-level `model` — evolution must not move a harness
    onto a pricier model, or a different lab, on its own initiative.
  - **Imperative**: `RunControl.switch_model(...)` and
    `POST /runs/{run_id}/model`, consumed at the loop's next turn boundary
    alongside stop and steer, where no model call or tool is in flight.
  - `hiveloom.models.router.ModelRouter` owns which model is current and which
    provider instance serves it, building cross-provider instances lazily so an
    unused provider's absent credentials are not a startup failure.
    `run_harness(providers={...})` pre-registers instances for a caller that
    wants to control what a swap talks to.
  - Prior turns are stripped of model-internal content at the boundary
    (`thinking`, `redacted_thinking`, anything carrying a `signature`), because
    those blocks are only valid for the model that produced them. An assistant
    turn stripped to nothing is dropped whole rather than left to break role
    alternation.
  - Swaps are journalled as `model_swap`; a swap to an unknown provider is
    `model_swap_failed` and the run continues on the model it had.
- **Evidence integrity for swapped runs** — `run_finished` carries `model_path`
  and `models_used`, `runs` gains a `model_path` column, and
  `Hive.version_stats` **excludes** runs that changed models from the
  per-version fitness bucket, reporting the count held out as `swapped_runs`.
  A run whose model moved did not execute the harness as declared, and
  averaging it into "this version scores N%" would silently turn that number
  into a distribution over model paths. `hiveloom stats` says so in words and
  breaks the held-out runs out into a per-model-path table;
  `version_stats(include_swapped=True)` returns the raw population. An empty
  `model_path` (every pre-1.0 run) means "not recorded", never "swapped".
- **Run tasks are indexed.** `runs` gains `task` (the opening statement, capped
  at 2000 chars — a title and search target, not a shadow copy of the journal),
  and `search_runs()` finds runs by what was asked.
- **`Hive.compare_versions(name, left, right)`** puts two harness versions side
  by side with deltas (right minus left), plus which failure signatures stopped
  appearing and which started. It reports `underpowered` when either side has
  fewer than five runs, because a confident delta over a sample of two is worse
  than no delta.
- **Workbench evolution, comparison, and resume** (`devtools/ui`):
  `GET /api/harnesses/{id}/compare`, the proposal queue
  (`evolve/propose`, `proposals`, `proposals/{id}/apply`, `proposals/{id}/reject`),
  and `POST /api/harnesses/{id}/resume` to re-run a fork from the journal point
  it was created at. Apply treats an unlisted code change as pending: silence
  is a refusal, not consent.
- **Operator control for a live run**, the runtime half of a debugger UI.
  - `RunControl` steering messages are now **addressable**: `send_message`
    returns an id, and `pending_messages` / `edit_message` / `remove_message`
    let an operator see and correct the queue before the loop drains it. An
    edit rewrites in place rather than re-queueing, so it cannot silently
    reorder what the agent is told. The loop still receives plain strings.
  - `RunControl.switch_playbook(name, reason=...)` moves a running harness into
    another mode at its next turn boundary. The mode's own `on_enter`/`on_exit`
    gates still run and may refuse — an operator switch goes through the same
    door the model uses, not around it — and the model is told, since a mode
    change it cannot see is one it will misread. Traced as `playbook_switch`
    with `source: "operator"`, or `playbook_switch_failed`.
  - `runner.new_run_id()` is public: pre-allocating the id is how a caller
    addresses a run while it is still going.
- **Context meter** — `model_call` carries `input_tokens` and
  `max_input_tokens`. Both were already known at that point (the first is
  counted for the cost guardrail), so a reader can show how close a call ran to
  its budget without re-tokenizing the conversation.
- **`file_write` declares what it produced** — an `Artifact(kind="file")` with
  the path, `created`/`modified`, size, sha256, and the previous size and
  sha256 when it overwrote something. A write is both a deliverable and a
  mutation, and neither was recoverable from the result string. Hashes rather
  than contents: the new content is already in the call's input, and the
  before-hash is the one thing the journal genuinely could not otherwise say.
- **Workbench run control** (`devtools/ui`) — the run stream
  now opens with `run_accepted` carrying a pre-allocated id, and
  `POST /api/runs/{id}/` `stop` · `messages` (with `GET`, `PATCH`, `DELETE`) ·
  `model` · `playbook` address a run while it is still going. Plus
  `POST /api/runs/{id}/fork`, `GET /api/runs/{id}/export` (the journal bytes
  verbatim — a re-serialization would not verify against the hash chain), and
  `GET /api/runs/live`. A fork's target name is slug-checked and resolved
  beside the parent harness: a fork writes files, and a browser must never
  choose where.
- **Fork x swap: the controlled A/B** — `hiveloom fork <run_id> --at <seq>
  --model <id> [--provider <name>]` replays one exact prefix on a different
  model. The override rewrites the fork's *spec* (through `construct.set_model`,
  so it is validated and rolled back like any other edit) rather than swapping
  mid-run, which means each arm is a clean sample of its own harness version
  and neither is held out of a fitness bucket as swapped. `hiveloom lineage`
  shows both arms with their version hash and the model each executed on.
  A rejected model removes the half-built fork rather than leaving one behind
  that claims a model it does not have.
- **Fork trust inheritance** — a fork whose files came from an already-trusted
  source folder, verified against the journal's manifest, inherits that trust:
  it is the same code at a new path. A fork built from a journal's inlined
  `contents` never does — a journal is a file someone can hand you, and its
  hash chain proves internal consistency, not provenance — and says so.
- **Lineage** — `runs` gains `parent_run_id` and `forked_at_seq` (migrated in
  place on existing Hives), and `hiveloom lineage <run_id>` shows the fork tree
  with each fork's divergence point, so a fork and its parent are compared on
  the prefix they share rather than as two unrelated runs.

### Fixed

- Fork snapshots now include playbook prompt files, boundary hooks, and
  playbook-local validators in the behavioral manifest. Forks with playbooks
  therefore validate and resume instead of losing the prompt files they need.
- `hiveloom evolve --model provider/model` loads the harness spec before
  resolving the proposing model, so providers declared by that harness's own
  extensions work for evolution as well as execution.
- Byte-identical local extensions copied into a harness fork are idempotent in
  a long-lived workbench process; the parent and fork can coexist without
  colliding with their own provider/tool registrations. Different extension
  bodies still collide loudly.
- The workbench now calls the fork-resume API and preserves provider identity
  in cross-provider fork model selectors.
- The workbench recursively discovers this checkout's `harnesses/` tree by
  default (plus optional `--scan-dir` roots), and UI-created forks now live
  under their source harness at `.hiveloom/forks/<name>` instead of appearing
  as unrelated sibling directories.
- The workbench fork dialog declared `model_override` as a string while the API
  returns an object, and rendered it straight into JSX — a fork created with a
  model override crashed React. The type now matches the API and the override
  renders as `from -> provider:model`.

- **`claude-opus-5` was missing from the builtin model catalog**, so a fresh
  install rejected it in `harness.yaml` even though `models/claude.py` already
  handled its API surface — the provider layer and the catalog had drifted
  apart. Added at its published rate, along with `claude-mythos-5`. A test now
  asserts that every model prefix `models/claude.py` special-cases is one the
  catalog knows, so the two cannot drift again. (Sonnet 5's promotional
  introductory rate is deliberately *not* used: a harness folder outlives the
  promotion, and over-estimating cost only makes the cost guardrail halt
  sooner.)
- **`hiveloom --version`** now exists (`-V` too). The install documentation has
  told new users to run it as their first verification step since 0.4; it
  exited `2` with "No such option".

- **`hiveloom add playbook`** now exists. Playbooks were reachable only from
  the SDK or the generator, so the documented "never hand-edit `harness.yaml`"
  rule had no CLI path for them. Carries `--tools`, `--model`,
  `--model-provider`, `--on-enter`, `--on-exit`, and `--entry`.

### Changed

- **Documentation for the 1.0 surface.** Two new reference pages —
  [`docs/journal.md`](docs/journal.md) (the journal's shape, `trace --verify`,
  `--materialize`, forking, `--resume`, lineage, model hot-swap, and why a
  swapped run is held out of its fitness bucket) and
  [`docs/workbench.md`](docs/workbench.md) (the development UI). The README
  gains a workbench section and a journal section; `docs/architecture.md` gains
  the journal, `fork.py`, `models/router.py`, and `loop/control.py`; the
  `hiveloom-run` skill gains the fork-to-debug loop. `scripts/sync-docs.sh` in
  the hiveloom-cloud checkout publishes all of it to docs.hiveloom.cloud.

- **BREAKING** — `logging.level` values are renamed for what they cost you:
  `full` -> `journal` (forkable) and `tool_calls_only` -> `summary` (no context
  bodies, **not** forkable). The old names still load, in both `harness.yaml`
  and `TraceWriter`, so existing harness folders keep working.

- Distribution metadata is now complete for a PyPI release: `[project.urls]`
  gives the project page its Homepage/Repository/Documentation/Changelog/Issues
  links, and the classifier list gains `Operating System :: OS Independent` and
  `Programming Language :: Python :: 3`. No `License ::` classifier is added on
  purpose — PyPI rejects a distribution that carries both a classifier and the
  PEP 639 `License-Expression` this project already declares.
- The README is the published long description, and PyPI does not resolve
  relative paths, so every in-repo link and image now uses an absolute GitHub
  URL. Behaviour on GitHub is unchanged. The install section leads with
  `uv add hiveloom`.
- The release workflow fails with an instruction when it is dispatched
  against a branch instead of a release tag, rather than reporting a
  mismatch between the branch name and the project version.

## [0.5.0] - 2026-08-15

### Added

- **Run control**: a thread-safe `RunControl` channel (`hiveloom.loop.control`)
  lets a caller stop a running loop gracefully or inject steering messages,
  both consumed at the next turn boundary. A stopped run completes with the
  new status `"stopped"`, trace intact and partial output kept. `hiveloom
  serve` exposes it as `POST /runs/{run_id}/stop` and
  `POST /runs/{run_id}/messages`, and streaming responses now open with a
  `{"type": "run_accepted", "run_id": …}` line so clients can address a run
  while it is still going. `runner.run_harness` accepts `control=` and a
  pre-allocated `run_id=`. Steering messages are traced as `user_steer`.
- **Truncation guard**: tool calls from a `max_tokens`-truncated model
  response are no longer executed on partial arguments — each call gets an
  error result asking the model for a compact retry, traced as
  `tool_truncated`.
- **Playbooks**: named modes a harness switches between during a run. A
  `playbooks:` entry carries a prompt fragment, the tool subset that mode may
  use, mode-scoped validators, and optional `on_enter`/`on_exit` code hooks.
  Declaring any auto-adds a `switch_playbook` tool. One harness with three
  playbooks replaces three harnesses that would otherwise duplicate a system
  prompt and split their evidence three ways.
  - `on_enter` may return `{"context": str}` to inject a note or
    `{"block": True, "reason": str}` to refuse entry (e.g. stale data);
    `on_exit` may refuse the *exit*, making it a boundary gate — "you entered
    targeting and proposed nothing". A stuck exit gate is force-released after
    three consecutive refusals, and the release is traced.
  - Every switch is a `playbook_switch` trace event and a
    `playbook_enter`/`playbook_exit` lifecycle event. The Hive indexes them, so
    `stats` reports success, cost, turns, and refusals **per playbook**, and
    the failure report localizes a problem to one mode.
  - `playbooks.*.on_enter`/`on_exit` execute code and are frozen from
    evolution, enforced by a value-inspecting gate check so rewriting the whole
    `playbooks` list cannot smuggle a hook in. Prompts stay mutable — evolution
    rewrites guidance, never side-effecting code.
  - Construction: `construct.add_playbook` scaffolds the prompt, hooks, and
    validators through the same validate-and-rollback path as everything else;
    because playbooks live in `spec/schema.py`, the JSON schema, annotated
    template, `explain`, and the generator's meta-prompt all pick them up.
- **Multi-turn conversation input**: `run_harness(conversation=[...])` and
  `dry_run(conversation=[...])` take a whole alternating thread. The trailing
  user message becomes the task statement; earlier turns are seeded as history
  and are the first thing compaction reclaims. For callers that own the
  conversation themselves, such as a chat service replaying it each turn.
- **Structured artifacts**: a tool may return a `ToolResult` carrying
  `Artifact(kind=..., data=...)` side-products. They surface on
  `RunResult.artifacts` (and `artifacts_of(kind)`), on the run payload, and on
  the trace — so a harness can drive a real UI with charts or proposals
  without pushing JSON through the model's text channel. Visible to guardrails
  via `RunState.artifacts`.
- **Per-run context injection**: `run_harness(context={...})` passes
  caller-owned values (a DSN, request-scoped state, a mutable accumulator) to
  code tools that declare a `run_context` parameter, and to validators. The
  parameter is hidden from the model's tool schema and cannot be forged by a
  model-supplied key of the same name. Never written to the trace.
- **`load_skill` builtin tool**: progressive disclosure without shipping a
  filesystem reader. Only skills the spec declares are reachable, and the
  schema enumerates them. The skills index points at whichever loader the
  harness actually carries.
- **Deferred outcome labels**: `Hive.record_outcome(run_id, "success"|"failure")`
  and `hiveloom outcome <run-id> <result>` attach a real-world judgement to a
  completed run — a human confirmed or dismissed the proposal, the extracted
  record turned out wrong. Validators grade a run while it happens; this
  records what the world said afterwards. Surfaced by `stats` and by the
  failure report, so it can drive evolution. The run row is never rewritten.
- **Artifacts over MCP.** An MCP server can hand hiveloom caller-facing
  side-products by returning structured content under a `_hiveloom` envelope
  (`{"_hiveloom": {"artifacts": [{"kind": ..., "data": ...}]}}`); the bridge
  lifts them onto `RunResult.artifacts` and keeps the envelope out of the
  model's text. Without this, moving a tool behind MCP silently downgraded it
  to text-only — a domain tool that also drives a UI (a chart spec, a decision
  proposal) could not be hosted remotely.
- Playbook tool subsets may name MCP tools (`mcp__<server>__<tool>`). They are
  exempt from the declared-tool check, since MCP tools are discovered from a
  live server when the registry is built and cannot be known at parse time.
- `ToolResult.retryable`: set `False` on a deterministic error so
  `loop.on_tool_error: retry_once` does not repeat it. A refused playbook
  switch uses it — retrying re-runs the gate's side effects and double-counts
  the refusal in run evidence.
- Two evaluation suites that measure the harness on frontier models, where
  article-extractor only measured the rescue of a weak one. `evals/article-digest`
  (output-heavy digest, Opus 5 / Sonnet 5) shows verification and retry closing
  the contract-compliance tail: 80% → 100% success at flat cost per success.
  `evals/page-audit` (exhaustiveness past a truncated tool view, aggregation,
  date arithmetic) shows the property that matters downstream: silently wrong
  runs drop from 5/6 and 3/6 raw to 0/6 harnessed.
- `evals/README.md`: an index of the three suites, the shared method, and the
  caveats that apply to all of them.
- `docs/models.md`: how the `claude` provider handles adaptive-thinking models.
- Charts for the README's evidence section, regenerated from the committed
  results by `docs/assets/make_plots.py`.

### Changed

- `model.temperature` is now optional and unset by default: when unset it is
  omitted from provider API calls entirely. Current Anthropic models reject
  the parameter as deprecated, so specs should simply not set it.
- The README's article-extractor table showed the pre-0.3.0 sweep, not the
  0.3.1 re-run it linked to. Corrected against the committed `RESULTS.md`
  (Haiku 2%→3% raw and 61%→65% harnessed, cost per success $0.3405→$0.2212
  raw, so 12× rather than 17×), with the Gemma arm — where the harness is
  slightly worse — added rather than omitted, and the statistical caveat that
  only the Haiku delta survives a paired test.

### Fixed

- `dry_run` reported `spec.system_prompt` verbatim instead of the assembled
  system prompt, omitting the skills index and active tools' guidelines. Both
  the reported prompt and `estimated_input_tokens` under-reported what a real
  run sends.
- `_set_dotted` (evolution's change applier) could not address list entries, so
  a dotted path through any list-valued section silently created a mapping and
  the whole batch was then rejected as invalid. Numeric segments now index
  lists, which is what lets evolution target one playbook's prompt.
- `construct._scaffold_hook` raised a bare `ValueError` on a malformed
  `path.py:function` ref instead of an actionable `SpecError`.
- `hiveloom.serve`'s `result_payload` was a second copy of the canonical run
  payload, so fields added to it (notably `artifacts`) were silently missing
  over HTTP. It now delegates.
- `claude` provider: omit `temperature` for the models whose API rejects
  sampling parameters (Opus 4.7 and later, Sonnet 5, Fable/Mythos), even when a
  spec sets one explicitly. Any value at all makes those models 400.
- `claude` provider: preserve `thinking` / `redacted_thinking` blocks on the
  assistant turn. Adaptive-thinking models require the turn to be replayed
  unchanged, so dropping them broke the next call of a tool-use loop.

## [0.4.0] - 2026-08-03

### Added

- Builtin model providers for the major labs, so no configuration is needed
  beyond an API key: `openai`, `gemini`, `mistral`, `deepseek`, `xai`, `groq`,
  `openrouter`, `together`, `fireworks`, `ollama`, and `vllm` join `claude`.
  All are backed by the existing stdlib-only OpenAI-compatible provider.
- `hiveloom models [PROVIDER] [--json]` lists providers with their endpoint,
  API-key variable, whether that key is set (never its value), catalog policy,
  and per-model pricing.
- `hiveloom set model <provider>/<model-id>` (and `construct.set_model`) moves
  provider and id in a single validated commit. Previously a harness could not
  change lab at all: the two fields validate against each other, so either
  single-field ordering was rolled back.
- Open-catalog providers: every builtin except `claude` accepts model ids that
  are not pre-registered, so a model released after a hiveloom version is
  usable immediately. `claude` stays closed, so typos still fail validation.
- `~/.hiveloom/models.yaml` can now extend a builtin provider (omit `base_url`
  to add models or correct pricing) or override one (supply `base_url` to point
  a lab name at a gateway or proxy).
- `docs/models.md`: the full model/provider reference.
- Prompt caching. The `claude` provider marks the system prompt, tool list,
  and conversation tail as cache breakpoints on every call, so the stable
  prefix of an agent loop is written once and read at 0.1x the input price on
  every later turn. Cache read/write tokens are broken out on `Usage`, priced
  in cost estimation (0.1x / 1.25x input), and visible in traces; OpenAI-style
  `cached_tokens` are likewise split out of the prompt count.
- `hiveloom mcp serve DIR [DIR ...]`: expose harnesses as MCP tools over
  stdio — the agent-facing front door. Each harness becomes a `run_<name>`
  tool whose description is the harness description and whose result is
  structured (`status`, `output`, `reason`, `cost_usd`, `turns`, `run_id`,
  `verdicts`), so a calling agent can delegate a task and check the verified
  outcome instead of improvising. Caller input is always literal text
  (mirroring the HTTP servers), trust is enforced per directory at startup,
  and failed runs land in the Hive like any other — so delegated work drives
  evolution too. SDK: `hiveloom.serve.mcp.build_mcp_server`.
- Local harness registry and MCP discovery. `hiveloom registry add|remove|list`
  keeps a machine-level list of harnesses (`~/.hiveloom/registry.yaml`);
  `hiveloom mcp serve --registered` serves all of them at once (broken entries
  are skipped with a stderr warning), and every MCP server now exposes a
  `list_harnesses` tool returning the catalog *with measured fitness* — total
  runs, success rate, average cost/turns from the Hive — so a calling agent
  can pick a harness on evidence, not vibes. Registration is not trust: the
  same per-directory trust gate still applies.
- `hiveloom mcp serve --http [--host] [--port]`: the MCP server over the
  streamable-HTTP transport at `/mcp`, gated by `HIVELOOM_API_KEY` (Bearer or
  `X-API-Key`, constant-time compare, mirroring `hiveloom serve`). A
  non-loopback bind without the key is refused at startup. No TLS — a gateway
  should front this, as with the other HTTP surfaces.
- Parallel tool execution (opt-in): `loop.tool_execution: parallel` preflights
  guardrails and hooks for a turn's tool calls in source order, executes the
  surviving calls concurrently, and finalizes results in source order.
  `TraceWriter` is now thread-safe, and the builtin `file_write` serializes
  writes per resolved path. The default stays `sequential`, where a halt
  during one call still prevents later calls from executing at all.
- Provider middleware events: `before_provider_request` lets a hook patch the
  outgoing request (`system`/`messages`/`tools`; this request only, and always
  after guardrails), and `after_provider_response` observes the wire-level
  accounting (`usage`, `cost_usd`, `stop_reason`) per call.

- Context-overflow recovery. When a provider rejects a request because the
  prompt exceeds the model's context window (the offline token estimate
  under-counted), the loop now force-compacts the history — targeting half the
  current estimate — and retries the turn once, instead of ending the run as an
  `error`. Recovery is traced as `context_overflow_recovery`; a second
  overflow still surfaces as an error run. Overflow rejections are classified
  by both the Anthropic and OpenAI-compatible providers
  (`ContextOverflowError`), and are never blindly retried against the same
  too-long prompt.

### Changed

- `summarize` compaction now requests a structured summary (Goal / Progress /
  Key decisions / Next steps / Critical context) instead of free-form "be
  terse" notes, so post-compaction turns keep direction, not just facts.
- Cost estimation resolves an unregistered model id through its provider's
  default price before the conservative fallback, so an unlisted model on a
  local Ollama or vLLM server is priced at zero instead of Haiku rates. Unknown
  *hosted* models keep the conservative fallback, so budget guardrails still
  never under-count.

### Fixed

- `evolve --apply` now restores the harness folder when validation of an
  approved proposal fails, instead of leaving a mutated spec, a bumped
  `# evolved:` counter, and half-applied code files behind.
- Failure analysis groups verdicts by a normalised template and counts distinct
  runs, so a behaviour that recurs across different inputs shows up as one
  cluster rather than one cluster per input. Analysis is now scoped to the
  current harness version.
- `openai_compat` reports the attempt a model call actually failed on, rather
  than claiming the full retry budget was spent on errors it never retried.
- `evals/article-extractor/scripts/setup_harnesses.sh` creates `harnesses/`, so
  the benchmark can be set up from a fresh clone.

## [0.3.1] - 2026-07-30

### Changed

- Simplified the Python generation SDK: `generate_harness(task, output)` now
  resolves the default strong model like the CLI, while dependency injection
  through `model=` remains supported.
- Added the missing `--json` startup contract to `control-plane`, aligning the
  long-running HTTP commands with the rest of the agent-facing CLI.
- Replaced the single internal structured-logging dependency with the standard
  library logger and added an 85% branch-coverage quality gate.
- Tightened source-distribution exclusions so local traces, caches, generated
  Docker artifacts, lock files, and embedded runtime wheels cannot ship.
- Simplified the README around hiveloom's durable-harness moat and added the
  reproducible article-extractor benchmark results.

## [0.3.0] - 2026-07-30

### Added

- A Hive-backed evolution proposal queue with create/list/show/apply/reject
  flows in the CLI and HTTP control plane.
- The `sequential_steps` loop policy for executing declared `loop.steps` in a
  fixed order.
- Opt-in post-run `evolution.auto_propose` drafting with failure thresholds,
  cooldowns, and proposal deduplication; proposals are never auto-applied.
- MCP server declarations, construction commands, dynamic tool discovery,
  runtime dispatch, and `hiveloom mcp list-tools`.
- A bearer-authenticated, non-production HTTP control plane with ed25519 key
  generation, authorization, signing, bounded run concurrency, and
  run/evolve/proposal endpoints.
- Typed-package metadata (`py.typed`) and SPDX license metadata for PyPI.

### Changed

- Proposal records expose typed proposal/gate/apply-result accessors; queue
  deduplication happens before the strong-model call, and proposal application
  claims are concurrency-safe and retryable.
- The HTTP mutation boundary is derived from `ALWAYS_FROZEN`, and remove
  checks derive list-section roots from the construct API.
- Starlette, Uvicorn, PyJWT with crypto support, and cryptography are direct
  runtime dependencies for the control plane.
- `hiveloom keys authorize` now requires an explicit `--scope` (no `*` default),
  so a key is never granted broad scope by omission.
- Bearer tokens have a 7-day server-side lifetime ceiling enforced at verify
  time, in addition to the 15-minute default TTL.
- The release workflow no longer publishes to PyPI automatically on a tag push;
  a `v*` tag builds and cuts a GitHub Release, while publishing is a deliberate
  manual `workflow_dispatch` with `publish_pypi=true`.

### Fixed

- OpenAI-compatible responses now preserve reasoning-only text, never
  re-serialize empty assistant turns as `content: null`, tolerate dict-shaped
  tool-call arguments, fall back across usage-token field names, and map
  `content_filter` termination. Tool-call arguments that are valid-but-non-object
  JSON (`"null"`, `"[1]"`) and explicit null usage counts no longer crash
  normalization.
- The HTTP control plane freezes the code-execution roots `tools` and
  `verify.validators` and the `name` root over `mutate` scope (closing a remote
  code-execution and a cross-harness rebinding path), and refuses writes to a
  parent of any frozen leaf.
- MCP server names may not contain `__`, keeping the `mcp__<name>__<tool>` tool
  prefix injective so tools from different servers cannot silently collide.
- Auto-propose records a terminal attempt when a draft gates to nothing, so the
  cooldown advances and a failing run no longer re-pays a strong-model call.
- `authorized_keys.json` rows that are non-dict or missing `key_id` fail closed
  as a typed 401 instead of an unhandled 500.
- `hatchling` is pinned to `>=1.27` for the SPDX license metadata it emits.
- Loop-policy dependency injection again distinguishes an omitted policy from
  an explicitly supplied one.
- Auto-propose cooldowns have a real one-minute minimum instead of accepting
  functionally disabled near-zero values.
- MCP transport declares its HTTP dependency, bounds initialization time, and
  rejects tool-name collisions introduced by sanitization.
- The HTTP control plane fails closed on corrupted authorized-key rows and
  blocks sensitive input-file reads, case-variant path bypasses, custom trace
  directory leaks, and mutation access to frozen configuration.
- Proposal queue review fixes cover trust checks, stale specs, confirmation
  ordering, code-path approval, apply claims, and harness-bound HTTP access.

## [0.2.0] - 2026-07-21

### Added

- Deployable analysis and extraction example harnesses alongside the summarizer.
- Per-lifecycle agent skills (`skills/hiveloom-{build,run,evolve,extend,ship}`) for
  progressive disclosure, in addition to the all-in-one `SKILL.md`.
- Atomic spec writes: construction steps validate and roll back on failure instead
  of leaving a harness directory in a partially-written state.
- Trust gating for foreign harness folders — `hiveloom trust`, `--approve`, and
  `HIVELOOM_TRUST` now gate any foreign code before it loads.
- WAL-mode Hive storage with explicit pruning and trace-level honoring.
- CI, security audit, release, and Dependabot workflows.

### Changed

- Guardrails: `add guardrail` now replaces an existing singleton entry instead of
  appending a duplicate.
- Schema validation bounds runtime limits and validates model ids against
  registered providers.
- Evolve gates dangerous mutations and sandboxes code-change paths — regenerated
  code hooks always require explicit approval.
- The agent loop guards every model call and makes run cost/turn accounting precise.
- Model providers retry transient failures without losing usage accounting.
- Packaging excludes every `.env` variant and trace data from built artifacts.
- Shell and `http_get` builtin tools hardened (shell allowlists now match exact
  argv, not just `argv[0]`).

### Fixed

- The CLI translates unexpected errors into a clean message instead of leaking
  raw tracebacks.

## [0.1.0] - 2026-07-16

Initial beta release: the harness spec and loader, explore/construct CLI
commands, the runtime (`run`), the Hive (`trace`/`stats`), generation and
evolution (`generate`/`evolve`), and packaging (`package`).
