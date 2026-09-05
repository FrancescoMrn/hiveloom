# Task confinement

**A hiveloom harness confines an agent to one job and makes success provable.**

General-purpose agents are designed to accept many kinds of work. A hiveloom
harness takes the opposite approach: start with one repeatable task, give the
model only the capabilities it needs, bound its autonomy, and define how the
runtime will decide whether the result is acceptable.

The harness is a folder because the boundary must survive beyond one
conversation. It can be reviewed, committed, deployed, measured, and improved
like code.

## Agent = model + harness

The model supplies general reasoning. The harness supplies the task-specific
system around it: instructions, tools, reference material, context policy,
control loop, limits, and acceptance checks. Together they form the agent that
actually performs the job.

Hiveloom is agent-native at both ends of that lifecycle:

1. A capable **builder agent** can inspect the machine-readable schema and
   catalog, then create the harness through the JSON-speaking construction CLI.
   The same validation and rollback rules apply whether the author is a human,
   a coding agent, or the built-in generator.
2. A smaller **executor model** runs the resulting task many times inside the
   declared boundary. It no longer has to rediscover the workflow, tools, output
   contract, and recovery policy on every invocation.
3. A calling **agent or application** can invoke the finished harness through
   MCP, HTTP, the CLI, or the Python SDK and receive a validator-checked status.

This separates expensive, broad reasoning at build and improvement time from
cheap, bounded execution in the hot path.

## What the harness confines

| Boundary | What declares or enforces it |
|---|---|
| **Purpose** | The task input and `system_prompt` define the job. |
| **Capabilities** | `tools`, `skills`, `playbooks`, hooks, and `mcp_servers` define what can enter the loop. |
| **Autonomy** | Loop limits, context budgets, guardrails, tool policies, and cost ceilings bound execution. |
| **Acceptance** | Validators check the final output; `require_verification` prevents an unchecked result from becoming success. |
| **Change** | `mutable`, `frozen`, and always-frozen fields limit what evolution may propose or apply. |
| **Evidence** | The harness version hash and run journal bind each result to the spec and code that produced it. |

These controls live in the runtime, not only in instructions to the model. A
model cannot declare its own answer valid, raise its cost ceiling, add a tool,
or unfreeze an evolution field during a run.

## Prompt injection: confine the consequences

Hiveloom does not claim to reliably classify text as a prompt injection. That
is an arms race, and a missed instruction is enough. It instead treats task
input, retrieved documents, and tool results as untrusted and limits what an
obedient model can do with them:

- injected text cannot add a tool, widen a tool's scope, authorize a spill
  handle, change a frozen safety field, or declare its own output valid;
- prefer narrow, typed tools whose authorization comes from run context rather
  than model-supplied arguments;
- the final provider request is screened for known credential shapes plus the
  harness's configured redaction patterns, including changes made by request
  hooks; matches are redacted or block the request;
- shell is an explicit escape hatch. An allowlisted general-purpose reader can
  still traverse files without naming a blocked path. If untrusted content and
  runtime-state confidentiality meet in a shell-enabled harness, either remove
  that capability or opt into `confinement.mode: require`.

The useful guarantee is therefore not “the model ignored the injection.” It is
“the injection could not grant itself undeclared authority, and the output was
still checked before success.”

## Completion is a verdict

Model completion and task success are different events. The model may produce a
final answer, but hiveloom reports success only after the configured validators
pass. A failed check can return precise feedback to the loop for a bounded retry;
if the checks still fail, the run exits as `verify_failed` rather than quietly
returning a wrong result.

That distinction is what makes a harness useful to downstream automation. Its
caller receives an explicit status with stable exit semantics instead of having
to trust confident prose.

## More than a prompt, narrower than a general agent

A prompt describes desired behavior. A harness also supplies and enforces the
surrounding system:

- the exact tools and reference material available to the model;
- the context assembly and compaction policy;
- the maximum turns, wall-clock time, and spend;
- deterministic output checks and retry behavior;
- a journal of what actually happened;
- a controlled path for changing the harness from measured failures.

The goal is not maximum autonomy. It is the minimum autonomy that reliably
completes one checkable job.

## What task confinement does not mean

Task confinement is not, by itself, a virtual-machine boundary. Builtin file
tools are rooted to the harness directory, shell is disabled until an allowlist
is configured, sensitive paths are blocked, and foreign harness folders are
trust-gated before their Python hooks load. Those controls constrain the
model-facing runtime surface.

Processes the runtime *spawns* — the `shell` tool and the `command_succeeds`
validator — always get a scrubbed environment, resource limits, bounded output,
and a timeout that kills the whole process tree. OS isolation is optional:
`auto` uses `bwrap` on Linux or `sandbox-exec` on macOS when available and
otherwise continues with those portable controls; `require` is the explicit
fail-closed choice; `off` skips discovery. `hiveloom confinement` reports what
a given machine will actually enforce, and the run journal records which
backend was used. See [the `confinement` section](spec.md#process-confinement).

That boundary stops at the process the runtime starts. Custom Python hooks and
extensions execute inside the hiveloom process itself, and declared MCP servers
perform their own local or network I/O — both by design, since both are the
harness author's own code behind the trust gate. Run untrusted harnesses inside
an appropriate container or operating-system sandbox; the packager can produce a
container image, but deployment policy remains an operator responsibility.

## When a task fits

Hiveloom is a good fit when a task is:

- repeated often enough to justify a durable artifact;
- narrow enough to state as one job;
- checkable with a schema, code validator, command, file assertion, or another
  deterministic signal;
- valuable enough that silent failure, runaway cost, or configuration drift
  matters.

One-off creative work and tasks with no meaningful acceptance signal are
usually better handled directly by a general-purpose agent.

## The lifecycle preserves the boundary

1. **Construct:** a human, coding agent, or generator creates the harness
   through the validated construction API.
2. **Validate:** schema validation and a dry run catch invalid wiring before a
   model call.
3. **Run:** the executor works inside the declared tools, budgets, and loop
   policy; validators decide the final status.
4. **Inspect:** the journal records the exact version, context, calls, verdicts,
   cost, and stopping reason.
5. **Improve:** evolution reads measured failures but may change only approved
   fields; applying a proposal remains a separate human action.

The product is therefore not the conversation and not the model. It is the
bounded, portable, verifiable task contract around the model.
