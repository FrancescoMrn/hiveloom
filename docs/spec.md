# Harness spec reference

The harness spec is a declarative YAML document (`harness.yaml`) with code
escape hatches (`path/to/file.py:function`). It is defined by the pydantic models
in `src/hiveloom/spec/schema.py` — the authoritative, machine-checked source. Two
commands emit the contract directly from that schema, so this document can never
be the thing that drifts:

```bash
hiveloom schema --json        # the JSON schema
hiveloom schema --annotated   # a valid, commented YAML template
hiveloom explain <path>       # field docs, e.g. `hiveloom explain context.compaction`
```

## Sections

| Section | Purpose | Notable fields |
|---|---|---|
| `schema_version` | Harness document format | defaults to `0.2.0`; legacy `version` still loads and `hiveloom migrate HARNESS --json` rewrites it atomically |
| `name` / `description` | Identity (Hive + packaging) | required |
| `model` | The executor model | `provider` (builtin: `claude`), `id` (default `claude-haiku-4-5`), `max_tokens`, `temperature` (optional; unset = omitted from API calls — current Anthropic models reject it as deprecated) |
| `system_prompt` | System prompt for the executor | required; the evolver may rewrite it |
| `tools` | Tools available to the loop | list of `{builtin: name}` or `{code: path.py:fn, description: ...}` |
| `mcp_servers` | MCP servers whose tools join the loop | `transport: stdio\|http`; discovered eagerly (incl. `run --dry-run`); **always frozen** |
| `extensions` | Harness-local extension modules | paths/modules loaded before validation; **always frozen** |
| `skills` | Progressive-disclosure instructions | names of `skills/<name>/SKILL.md` folders |
| `playbooks` | Named modes the run switches between | `name`, `description`, `prompt` (md fragment), `tools` (active subset), `validators`, `model`/`model_provider` (**always frozen**), `on_enter`/`on_exit` (**always frozen**), `entry` |
| `hooks` | Lifecycle middleware | code or catalog handlers attached by `event` |
| `context` | Context assembly & budgeting | `max_input_tokens`, `strategy` (`rolling`\|`full`\|`summary`), `compaction.{trigger_at_pct,method}`, `pinned` |
| `guardrails` | Safety gates | list of builtins/code; **frozen from evolution** |
| `loop` | Loop policy & stop conditions | `policy` (`react`\|`plan_then_act`\|`sequential_steps`), `steps` (ordered objectives for `sequential_steps`), `max_turns`, `on_tool_error`, `require_verification` |
| `verify` | Verification (the reward signal) | `validators` (builtins/code), `on_fail.{action,max_retries}` |
| `logging` | Journal policy | `trace_dir` (in-folder by default), `level` (`journal`/`summary`), `snapshot_files`, `redact` (regexes; **frozen**) |
| `evolution` | What the evolver may change | `enabled`, `mutable` (paths it MAY change), `frozen` (paths it must NEVER change), `auto_propose.{enabled,min_failures,cooldown_hours,model}` (opt-in post-run DRAFT trigger — never auto-applies; `auto_propose` itself is never mutable) |

## Builtins

List them with `hiveloom catalog <tools|guardrails|validators|policies|compaction|hooks>`.

- **Tools:** `file_read`, `file_write` (sandboxed to the working dir), `shell`
  (allowlist-only, disabled without one), `http_get`, `load_skill` (reads a
  declared skill in full — progressive disclosure without a filesystem reader).
- **Guardrails:** `max_cost_usd`, `max_wall_clock_seconds`, `max_turns_hard_cap`,
  `tool_allowlist`, `no_network_write`, `regex_output_filter`. All but
  `regex_output_filter` are *singletons*: only one entry is meaningful, so
  `hiveloom add guardrail` replaces an existing one (including the injected
  default `max_cost_usd`) rather than appending a redundant second entry.
  `regex_output_filter` composes as a list — one entry per pattern.
- **Validators:** `output_schema` (JSON-schema check), `regex_match`,
  `file_exists`, `command_succeeds` (exit 0 = pass).
- **Policies:** `react`, `plan_then_act`, `sequential_steps` (walks the fixed,
  ordered `loop.steps` list, refusing completion until each is done in order).
- **Compaction:** `summarize`, `truncate_oldest`.
- **Hooks:** `strip_json_fence` (an opt-in final-output normalizer).

Code hooks are the primary extension point. A validator hook has the signature
`validate(run_output, run_context) -> {"passed": bool, "feedback": str}`; a tool
hook is any `@hiveloom.tools.tool`-decorated function (its JSON input schema is
derived from type hints). `hiveloom add …/--code` scaffolds a correctly-signed
stub.

A tool that declares a `run_context` parameter is handed the run context
(`input`, `harness_dir`, `run_id`, and the caller's own `context` dict from
`run_harness(context=...)`) instead of having the model supply it; the
parameter is hidden from the tool's JSON schema and cannot be forged by a
model-supplied key of the same name. A tool that returns a `ToolResult` may
attach `Artifact(kind=..., data=...)` side-products, which reach the caller on
`RunResult.artifacts` without passing through the model's text channel.

## Playbooks

A **skill** is reference material the model reads; a **playbook** is a
configuration the runtime applies. Entering one swaps in a prompt fragment,
narrows the active tools, and adds mode-specific validators — so one harness
covers what would otherwise need several, while keeping one conversation and
one evolving spec.

```yaml
playbooks:
  - name: overview
    description: Read the segment landscape. No actions.
    prompt: playbooks/overview.md
    tools: [run_sql, render_chart]
    entry: true

  - name: targeting
    description: Turn a cohort into a confirmable proposal.
    prompt: playbooks/targeting.md
    tools: [run_sql, render_chart, propose_decisions]
    validators:
      - code: validators/proposal.py:check_consent
    on_enter: hooks/refresh_features.py:run
    on_exit: hooks/require_proposal.py:check
```

Declaring any playbook auto-adds a `switch_playbook` tool, which stays active
in every mode — a mode the model cannot leave is a trap, not a mode. The run
starts in the `entry` playbook, or the first one declared.

**Gates.** `on_enter` and `on_exit` receive `{playbook, from/to, reason,
run_context}` and may return `None` to observe, `{"context": str}` to inject a
note into the conversation, or `{"block": True, "reason": str}` to refuse. A
blocking `on_exit` is a *boundary* check — the mode grades itself as the agent
leaves ("you entered targeting and proposed nothing") instead of waiting for
the end of the run. A gate that refuses three times running is force-released,
so a badly written gate cannot trap the run; the release is traced. A hook that
raises is recorded as `hook_error` and skipped, never crashing the run.
Refusals reach the model as a tool error and are not retried.

**Evidence.** Each switch is a `playbook_switch` trace event and a
`playbook_enter`/`playbook_exit` lifecycle event. The Hive indexes them, so
`hiveloom stats` breaks success, cost, turns, and refusals down per playbook,
and the failure report localizes a problem to one mode. Attribution is *by
visit*: a run that worked in two modes counts once for each.

**Its own model.** A playbook may declare `model:` (and `model_provider:`),
so a mode runs on a different executor: profile cheaply, decide expensively,
inside one harness and one conversation. Leaving the mode restores the
harness default — a mode is a configuration, not a one-way door. The switch
happens at a turn boundary, where prior turns are stripped of content only the
previous model can validate.

**Freeze.** `on_enter`/`on_exit` execute code, and `model`/`model_provider`
are the same cost-and-capability decision that already keeps top-level `model`
frozen. None can be changed by evolution, including through a rewrite of the
surrounding `playbooks` list. Prompts are the evolvable part — which is the
point: evolution rewrites one mode's guidance on that mode's own evidence.

## MCP servers

A harness can declare MCP servers; their tools become ordinary dispatchable
tools inside the loop, named `mcp__<server-name>__<tool>`.

An MCP tool can reach the *caller* as well as the model. Returning structured
content under a `_hiveloom` envelope —
`{"_hiveloom": {"artifacts": [{"kind": "chart", "data": {...}}]}}` — lands
those entries on `RunResult.artifacts` exactly as a local code tool's would,
and the envelope never enters the model's text. This is what lets a domain
tool that also drives a UI be hosted on a server instead of copied into every
harness that needs it. Discovery is
**eager** — it happens when the tool registry is built, which includes
`run --dry-run`. Dry-run never calls the model API, but a harness with
`mcp_servers` genuinely performs local/network I/O to discover their tools
(see AGENTS.md rule 5). `mcp_servers` is **always frozen** from evolution —
the same risk class as `extensions` (arbitrary code/process).

A stdio entry launches a local subprocess — **arbitrary local exec** —
gated by the same harness-trust boundary as any other code hook (see
`hiveloom trust`):

```yaml
mcp_servers:
  - name: search
    transport: stdio
    command: npx
    args: ["-y", "@foo/mcp-search"]
    env_from_host_env:
      API_KEY: FOO_SEARCH_API_KEY   # resolved from the host env at connect time
```

An http entry reaches a Streamable HTTP endpoint:

```yaml
mcp_servers:
  - name: jira
    transport: http
    url: https://mcp.acme.com/mcp
    header_env:
      Authorization: ACME_MCP_TOKEN
    tools: [search_issues, create_issue]   # allowlist; omit to expose all
```

Add one with `hiveloom add mcp-server` (see `hiveloom add mcp-server --help`);
inspect what a harness's declared servers actually expose with
`hiveloom mcp list-tools --dir ./h`.

## Three identities, three jobs

- `schema_version` identifies the `harness.yaml` document format.
- The behavior hash identifies the validated spec plus referenced prompts,
  hooks, extensions, skills, and output schemas.
- The execution fingerprint identifies one reproducible run configuration:
  behavior hash, Hiveloom runtime, requested and effective model identity,
  runtime overrides, input digest, model path, and lineage.

The legacy top-level `version` key remains readable for a transition. A
document containing conflicting `version` and `schema_version` values is
rejected. Run `hiveloom migrate HARNESS --json`; never edit either field by
hand. Migration validates hooks before and after its atomic write, restores the
original bytes on failure, and does not change the behavior hash.

## External run metrics

Numeric evaluator results are not part of `harness.yaml`. They are immutable
Hive records attached to an indexed `run_id`, with a user-defined name, finite
value, maximize/minimize direction, unit, source, and case/run/eval scope.
Inspect the machine contract with `hiveloom metrics schema --json`; record or
transactionally import observations with `hiveloom metrics record|import`, and
query them with `hiveloom metrics list`.

Metric aggregation keeps name, source, scope, unit, and direction separate and
reports both sample and missing-value counts. Binary `run_outcomes` remain the
contract for deferred success or failure and are not reinterpreted as numeric
metrics.

## Safety invariants (enforced in code)

1. The evolver can never modify `id`, `guardrails`, `model`, `logging.redact`,
   `extensions`, `hooks`, `mcp_servers`, or `evolution.auto_propose` — nor any
   playbook's `on_enter`/`on_exit`, including by rewriting the `playbooks` list
   around them. Playbook *prompts* stay mutable: evolution rewrites guidance,
   never side-effecting code.
2. Code-hook regeneration always requires explicit human approval.
3. `shell` is allowlist-only and disabled unless the spec enables it.
4. Redaction patterns are applied before any trace is persisted.
5. The cost guardrail defaults **on** (`max_cost_usd: 1.00`) even if omitted from
   a spec.

## The harness directory

```
<harness-name>/
├── harness.yaml          # the spec
├── tools/  validators/  schemas/  playbooks/
├── .hiveloom/
│   ├── traces/           # in-folder trace dir (memory travels with the harness)
│   └── forks/<name>/     # experiments on this harness (`hiveloom fork`)
├── .env.example          # every env var the spec/hooks reference
├── pyproject.toml        # PEP 621 deps: hiveloom==<pinned> + hook deps
└── README.md
```

The folder is portable and versionable, like a `docker-compose.yml`; it needs the
runtime (`pip install hiveloom`) wherever it lands. `hiveloom package` bundles it
into `<name>-<version_hash>.zip` (+ optional Dockerfile), excluding `.env` and
`.hiveloom/`.

A fork is a full harness directory of its own — spec, code hooks, and a
`fork.yaml` naming the run and seq it re-entered — kept under
`.hiveloom/forks/<name>` rather than beside the harness. It is an experiment
*on* this harness, not a harness of its own: archiving or packaging the folder
therefore leaves the experiments behind with the traces, a directory of
harnesses stays a directory of harnesses, and the file tools — rooted here and
not descending into `.hiveloom` — cannot read or mutate a running experiment.
Forking a fork produces a sibling under the same original harness rather than a
deeper nest; `fork.yaml` is the only record of generation.
