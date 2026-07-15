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
| `version` | Spec format version | defaults to `0.1.0` |
| `name` / `description` | Identity (Hive + packaging) | required |
| `model` | The executor model | `provider` (builtin: `claude`), `id` (default `claude-haiku-4-5`), `max_tokens`, `temperature` |
| `system_prompt` | System prompt for the executor | required; the evolver may rewrite it |
| `tools` | Tools available to the loop | list of `{builtin: name}` or `{code: path.py:fn, description: ...}` |
| `extensions` | Harness-local extension modules | paths/modules loaded before validation; **always frozen** |
| `skills` | Progressive-disclosure instructions | names of `skills/<name>/SKILL.md` folders |
| `hooks` | Lifecycle middleware | code or catalog handlers attached by `event` |
| `context` | Context assembly & budgeting | `max_input_tokens`, `strategy` (`rolling`\|`full`\|`summary`), `compaction.{trigger_at_pct,method}`, `pinned` |
| `guardrails` | Safety gates | list of builtins/code; **frozen from evolution** |
| `loop` | Loop policy & stop conditions | `policy` (`react`\|`plan_then_act`), `max_turns`, `on_tool_error`, `require_verification` |
| `verify` | Verification (the reward signal) | `validators` (builtins/code), `on_fail.{action,max_retries}` |
| `logging` | Trace policy | `trace_dir` (in-folder by default), `level`, `redact` (regexes; **frozen**) |
| `evolution` | What the evolver may change | `enabled`, `mutable` (paths it MAY change), `frozen` (paths it must NEVER change) |

## Builtins

List them with `hiveloom catalog <tools|guardrails|validators|policies|compaction|hooks>`.

- **Tools:** `file_read`, `file_write` (sandboxed to the working dir), `shell`
  (allowlist-only, disabled without one), `http_get`.
- **Guardrails:** `max_cost_usd`, `max_wall_clock_seconds`, `max_turns_hard_cap`,
  `tool_allowlist`, `no_network_write`, `regex_output_filter`.
- **Validators:** `output_schema` (JSON-schema check), `regex_match`,
  `file_exists`, `command_succeeds` (exit 0 = pass).
- **Policies:** `react`, `plan_then_act`.
- **Compaction:** `summarize`, `truncate_oldest`.
- **Hooks:** `strip_json_fence` (an opt-in final-output normalizer).

Code hooks are the primary extension point. A validator hook has the signature
`validate(run_output, run_context) -> {"passed": bool, "feedback": str}`; a tool
hook is any `@hiveloom.tools.tool`-decorated function (its JSON input schema is
derived from type hints). `hiveloom add …/--code` scaffolds a correctly-signed
stub.

## Safety invariants (enforced in code)

1. The evolver can never modify `guardrails`, `model`, or `logging.redact`.
2. Code-hook regeneration always requires explicit human approval.
3. `shell` is allowlist-only and disabled unless the spec enables it.
4. Redaction patterns are applied before any trace is persisted.
5. The cost guardrail defaults **on** (`max_cost_usd: 1.00`) even if omitted from
   a spec.

## The harness directory

```
<harness-name>/
├── harness.yaml          # the spec
├── tools/  validators/  schemas/
├── .hiveloom/traces/     # in-folder trace dir (memory travels with the harness)
├── .env.example          # every env var the spec/hooks reference
├── requirements.txt      # hiveloom==<pinned> + hook deps
└── README.md
```

The folder is portable and versionable, like a `docker-compose.yml`; it needs the
runtime (`pip install hiveloom`) wherever it lands. `hiveloom package` bundles it
into `<name>-<version_hash>.zip` (+ optional Dockerfile), excluding `.env` and
`.hiveloom/`.
