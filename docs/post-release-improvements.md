# Post-release improvements from the matching eval

Status: audited against Hiveloom 1.0.0. Implementation PRs may start.

Last reviewed: 2026-08-29.

Baseline inspected: Hiveloom 0.5.0, before the announced large release.

Release audited: Hiveloom 1.0.0 at `dbfdc3a3b93bcfbe4b811917e91033b4c635b826`.

This document preserves the findings from a real talent-matching eval and turns them into small future PRs. It is not an implementation spec for the current branch. The first task after the release is to test every item against the released code, remove work that is already done, and rewrite proposals whose assumptions no longer hold.

The raw dataset and traces are intentionally absent. They contain CV evidence and private application data. The numbers below are aggregate receipts from the eval runs.

## Release audit result

Hiveloom 1.0.0 adds 31,320 lines over 0.5.0. The clean release checkout passes 913 tests. It ships the tamper-evident run journal, stable harness IDs, model paths, model swapping, run forking and resume, playbook-local models and tools, artifact persistence, version comparison, and public names for several cross-module helpers.

The private eval's leakage audit passed on 10 sampled deals before the release smoke. One Regolo `qwen3.5-9b` case then ran against the clean 1.0.0 checkout: first-pass contract success, hallucination-free output, Recall@5 1.0, five tool calls, 12,209 input tokens, 614 output tokens, 38.17 seconds, and $0.001245 billed cost. This `n=1` run proves adapter compatibility only. It does not update the model comparison.

Those changes remove assumptions from five briefs, but none of the 18 outcomes is fully covered. The retained scopes are below.

| ID | Audit status | 1.0 evidence and retained scope |
| --- | --- | --- |
| PR-01 | Retained | Issue #33 remains open. Prompt files are hashed, but evolution still routes prose through `code_changes` and `approve_code`. |
| PR-02 | Retained, narrower | `new_run_id()` and literal SDK input exist. The CLI still has one heuristic `--input`; it exposes no run ID, trace destination, or non-mutating model override. `session_id` is removed from this brief because 1.0 removed sessions. |
| PR-03 | Retained | The 1.0 public-helper rename did not cover `_normalize`, `_to_openai_tool`, or `_to_openai_messages`. `ModelResponse` still carries no effective model, request ID, billed amount, or replay metadata. |
| PR-04 | Retained, composed with the journal | `run_finished` now stores output, verdicts, artifacts, `model_path`, and `models_used`. Public `RunResult` and CLI JSON still omit aggregate usage, effective identity, execution provenance, and first-pass recovery state. |
| PR-05 | Retained | The Hive indexes playbook visits and model paths. It still drops `tool_retry`, `tool_truncated`, compaction, steering, output-retry, and max-turn friction. Issue #31 remains open. |
| PR-06 | Retained, narrower | The journal cut one 12-call trace from 676 KiB to 184 KiB and supports regex redaction. Structured field redaction and a managed trace-file retention command remain absent. |
| PR-07 | Retained | Journals make bounded excerpts reproducible, but `FailureReport` still receives trace paths without reading them. Issue #30 remains open. |
| PR-08 | Retained | Auto-propose still gates only on `failure_count` and `min_failures`. Issue #32 remains open. |
| PR-09 | Partly shipped | Stable harness `id`, behavioral snapshots, and playbook-prompt hashing shipped. The spec format field is still named `version`, and no public execution fingerprint joins runtime, provider, effective model, and overrides. |
| PR-10 | Retained | Deferred binary outcomes and version comparison exist. The Hive has no numeric metric record or import surface. |
| PR-11 | Retained | The repository contains three purpose-built eval folders, but no scorer SDK or machine-readable eval spec in `src/hiveloom`. |
| PR-12 | Retained | Model routing records swaps after execution. It does not probe requested versus served identity or declared versus observed capabilities before an eval batch. |
| PR-13 | Retained, built on 1.0 primitives | Run IDs, journals, fork resume, and model paths can replace part of the proposed manifest. No native matrix runner or interrupted-batch resume exists. |
| PR-14 | Retained, narrower | `Hive.compare_versions()` compares binary fitness and failure signatures. It cannot aggregate arbitrary metrics, repeated trials, stability, or paired cost-quality deltas. |
| PR-15 | Retained | `SequentialStepsPolicy` still accepts `list[str]`. Its docstring states that v1 does not verify a step was done and leaves per-step validators to v2. |
| PR-16 | Retained, built on artifacts | Validator `run_context` now contains artifacts. It does not contain structured tool observations, and no `grounded_references` builtin exists. |
| PR-17 | Retained | Evolution reads failures, outcomes, and playbook stats. It has no metric objectives or regression evidence. |
| PR-18 | Retained | Generation and evolution prompts do not describe structured steps, grounding, eval objectives, or composite deterministic tools. |

### Decisions made by the audit

- PR-02 drops every session field. A model override changes the in-memory spec before the version hash and journal snapshot are built, so the Hive does not mix it with the configured model's runs.
- PR-04 extends the existing journal envelope instead of creating a second provenance log.
- PR-05 uses an additive `run_events` table. Per-run columns would lose event sequence, tool attribution, and future event types.
- PR-09 keeps `harness_id` as stable identity and `spec_version_hash` as behavioral identity. It adds `schema_version` as the document-format name and an execution fingerprint as run provenance.
- PR-10 keeps numeric metrics separate from binary deferred outcomes, joined by `run_id`.
- PR-13 uses the 1.0 run ID, journal, and Hive ingestion path. It does not copy or invent a second trace format.
- PR-14 extends the eval records introduced by PR-13 and reuses `compare_versions()` where binary fitness is enough.
- PR-16 extends the existing mapping-shaped validator context, preserving code validators that index it as a dictionary.

The implementation branches should use these scopes, not the pre-release draft where they differ.

## What the eval found

The eval exercised a Hiveloom harness against real search, ranking, verification, and final-answer behavior. It measured retrieval quality, output validity, grounding, stability, latency, and cost outside Hiveloom because the current run and Hive contracts cannot represent all of them.

| Run set | Final contract success | First-pass validity | Recall@5 | nDCG | Mean cost | Mean latency | Stability |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Qwen 3.5 9B, 30 cases | 30/30 | 30/30 | 0.267 | 0.136 | $0.00114 | 27.7 s | 0.871 Jaccard |
| GLM 5.2, 30 cases | 30/30 | 6/30 | 0.233 | 0.135 | $0.04625 | 55.1 s | 0.537 Jaccard |
| Qwen 3.8 27B, 10 cases | 7/10 | 0/10 | not used for comparison | not used for comparison | not used for comparison | not used for comparison | not used for comparison |

The Qwen 3.5 9B run also reached 0.500 longlist recall and 100% hallucination-free output. A Qwen 122B run matched its 0.267 Recall@5 at 14.6 times the cost. GLM 5.2 cost 40.5 times more than Qwen 3.5 9B in this run set.

These numbers do not rank the models in general. They describe one harness, one dataset, and one provider configuration. They are useful here because they exposed framework gaps that repeated across models.

### Recovery was hidden by final success

GLM 5.2 finished 30 of 30 cases successfully, but only 6 outputs were valid on the first pass. The remaining 24 needed an output-schema retry. Qwen 3.8 27B produced no valid first pass across 10 cases: 7 recovered and 3 ended as verification failures.

The final success field loses this distinction. A clean first pass, a recovered output, and a repeated failure have different cost and reliability profiles. Run results, traces, Hive indexes, reports, and evolution need to retain those states.

### Schema validity did not prove grounding

The external evaluator rejected three outputs that passed the JSON schema because they contained talent IDs absent from tool evidence. A generic grounding validator could have caught the same error inside Hiveloom by comparing selected references with observed tool results.

### The Hive could not retain the eval signal

The Hive contained about 199 matching runs and no `run_outcomes`. Recall, nDCG, longlist recall, hallucination counts, cost, and latency lived in the external evaluator. Hiveloom could neither query those numeric results nor use them when drafting an evolution proposal.

Binary deferred outcomes remain useful for eventual success or failure. They are too narrow for ranked retrieval, cost-quality tradeoffs, or paired model comparisons.

### Provider identity needed enforcement

The provider adapter caught 10 Apertus responses and 10 Mistral responses served under requests for Qwen 3.5 9B. Without checking the effective model returned by the provider, those runs would have been labeled as Qwen results.

The adapter also had to:

- map the user-facing `glm5.2` name to `glm-5.2`;
- preserve provider reasoning details between tool turns;
- control reasoning effort and routing options;
- read billed cost rather than estimate it from tokens;
- convert provider currency to the report currency;
- import private OpenAI-compatible helpers named `_normalize` and `_to_openai_tool`.

The generic provider contract does not need OpenRouter-specific policy. It does need enough public fields and codecs for an extension to implement this behavior without copying internals.

### The CLI made batch evaluation harder than the SDK

The eval adapter copied the harness to a temporary directory for every model, then ran `hiveloom set model` against the copy. It wrote long inputs to `input.txt` after a literal `--input` value caused `ENAMETOOLONG`. It parsed traces for token totals and the spec hash, read package and Git versions separately, copied traces to durable storage, and deleted the temporary harness. The Hive then retained trace paths that pointed into deleted directories.

The SDK already exposes some identifiers and paths that the CLI cannot set. The missing CLI surface forced state mutation and trace scraping for information Hiveloom already knew.

### Deterministic phases lived in extension code

The successful harness collapsed three upstream operations into one deterministic `search_and_verify_candidates` tool. Its provider adapter then exposed tools by phase:

1. Read the deal.
2. Search and verify candidates.
3. Produce the final answer with no tools available.

Runs consistently used three model turns and five upstream calls. The current sequential policy can describe steps as instructions, but it cannot enforce allowed tools, required calls, or per-step call limits. The workflow therefore lived partly in provider code, where Hiveloom could not inspect or verify it.

## Exit criteria for this roadmap

The roadmap is done when the matching eval can run without its framework workarounds:

- model and provider changes are runtime overrides, not temporary harness edits;
- literal and file inputs have separate CLI flags;
- run IDs, session IDs, traces, provenance, usage, and verification attempts are returned through public contracts;
- the effective provider model is checked before a batch is accepted;
- first-pass, recovered, and final failures remain distinct in the Hive;
- external numeric metrics can be attached to a run and queried;
- selected IDs absent from tool evidence fail verification;
- the three-phase tool workflow is declarative and enforced;
- traces can be redacted and retained without leaving stale paths;
- evolution can use bounded friction evidence and metric history without receiving a full private trace.

## Rebase audit after the release

The release audit is complete for the code and public contracts. The 30-run matching baseline can continue against the detached release checkout while implementation uses separate branches.

1. Release checkout: 1.0.0 at `dbfdc3a3b93bcfbe4b811917e91033b4c635b826`.
2. Offline baseline: 913 tests passed in 22.5 seconds.
3. Private data gate: 10 sampled deals passed with no detected leakage after one upstream timeout and a clean retry.
4. Live compatibility: one matching case passed on the first contract attempt against the detached release checkout.
5. Contract comparison: schema, catalog, CLI JSON, `RunResult`, `ModelResponse`, journal events, and Hive storage were inspected against all 18 briefs.
6. Roadmap issues: #30, #31, #32, and #33 remain open after the release.

The old eval result remains the comparison point, not a pass threshold. Provider behavior and model endpoints may differ in the 1.0 rerun.

## Design rules for the future PRs

- Keep one public contract change per PR when possible. The release is already large; this queue should remain easy to rebase and review.
- Add fields before requiring them. Existing harnesses, providers, traces, and Hive databases need a documented migration path.
- Keep matching logic outside core. Numeric metrics, grounding, step constraints, and provider metadata should remain domain-neutral.
- Treat final success and clean execution as separate facts.
- Redact evidence before it reaches traces, Hive indexes, excerpts, or evolution prompts.
- Keep automatic evolution proposal-only. Applying a proposal still needs an explicit command.
- Make every test credential-free and network-free. Provider cases use fixtures and fake providers.
- Update the schema, `--json` contract, docs, guides, examples, and changelog in the same PR as a public behavior change.

## Proposed PR queue

The IDs below are working labels. Final GitHub PR numbers will be assigned after the release audit.

| ID | Proposed PR | Depends on | Size | Existing issue |
| --- | --- | --- | --- | --- |
| PR-01 | Let evolution propose playbook prompt changes safely | release audit | M | #33 |
| PR-02 | Add explicit run inputs and runtime overrides | release audit | M | none |
| PR-03 | Expand the provider response contract and publish the OpenAI-compatible codec | release audit | M | none |
| PR-04 | Return a complete run execution envelope | PR-03 | M | none |
| PR-05 | Index recovered failures and other friction in the Hive | PR-04 | M | #31 |
| PR-06 | Add structured trace redaction and managed retention | PR-04 | L | none |
| PR-07 | Build bounded trace excerpts for evolution | PR-05, PR-06 | M | #30 |
| PR-08 | Trigger evolution proposals from repeated friction | PR-05, PR-07 | M | #32 |
| PR-09 | Separate schema identity from execution identity | release audit | L | none |
| PR-10 | Store numeric run metrics in the Hive | PR-09 | M | none |
| PR-11 | Add a scorer SDK and versioned eval spec | PR-04, PR-10 | L | none |
| PR-12 | Add provider and model capability probes | PR-03, PR-04 | M | none |
| PR-13 | Add a resumable native eval runner | PR-02, PR-11, PR-12 | L | none |
| PR-14 | Add eval reports and paired comparisons | PR-10, PR-13 | L | none |
| PR-15 | Enforce structured sequential steps | PR-09 | L | none |
| PR-16 | Add verification context and grounded-reference validation | PR-04, PR-15 | M | none |
| PR-17 | Let evolution reason over metric objectives | PR-07, PR-10, PR-14 | L | none |
| PR-18 | Teach generation and evolution the new workflow patterns | PR-15, PR-16, PR-17 | M | none |

Size is relative to this repository: `M` should remain one focused review; `L` probably needs an internal design note or a schema PR followed by behavior.

## Detailed PR briefs

### PR-01: Let evolution propose playbook prompt changes safely

Problem and evidence: playbook prompts affect behavior but live in referenced files outside `harness.yaml`. Evolution can identify a prompt problem but cannot propose the corresponding edit. This is tracked by issue #33.

Proposed contract:

- extend proposal artifacts with a `file_changes` collection;
- restrict targets to prompt files already referenced by the validated harness;
- store the expected pre-change digest with each file edit;
- show prose changes separately in `proposals show --json`;
- require `proposals apply --approve-prose` when a proposal includes prose;
- apply YAML and prompt edits in one transaction, with rollback on validation or write failure.

Compatibility: proposals without file changes keep their current shape and behavior. Old proposal files remain readable. This PR must not permit edits to hooks, extensions, tools, arbitrary paths, or files reached through a symlink escape.

Acceptance criteria:

- a prompt-only proposal can be created, inspected, applied, and rejected;
- stale file digests stop an apply and leave every file unchanged;
- path traversal and symlink escapes fail validation;
- a mixed YAML and prose proposal rolls back both parts when either part fails;
- the proposal records which prompt digest produced the subsequent run.

### PR-02: Add explicit run inputs and runtime overrides

Problem and evidence: `--input` guesses whether its value is a path. A long literal reached a filesystem call and failed with `ENAMETOOLONG`. Model matrices required a temporary harness copy plus `hiveloom set model` for each cell.

Proposed CLI:

```text
hiveloom run HARNESS --input-text "..." --json
hiveloom run HARNESS --input-file case.txt --json
hiveloom run HARNESS --model qwen3.5-9b --provider openrouter --json
hiveloom run HARNESS --run-id CASE_ID --session-id EVAL_ID --trace-dir PATH --json
```

`--input-text`, `--input-file`, and legacy `--input` are mutually exclusive. Model and provider overrides apply to the in-memory run configuration and never rewrite the harness. JSON output records requested and effective values.

Compatibility: keep `--input` for one deprecation cycle. Its implementation must detect a literal-looking or overlong value without calling a path API that can raise `ENAMETOOLONG`. SDK parameters remain valid.

Acceptance criteria:

- a multi-kilobyte literal input succeeds without a temporary file;
- a missing `--input-file` returns the documented validation exit code;
- runtime overrides leave every harness file byte-identical;
- custom run and session IDs reach the trace and Hive;
- a caller-selected trace directory produces durable paths after the process exits;
- CLI help and JSON error payloads explain conflicting input flags.

### PR-03: Expand the provider response contract and publish the OpenAI-compatible codec

Problem and evidence: the eval provider needed the effective model, request ID, billed amount, currency, reasoning replay data, and generic routing metadata. It imported private helpers to normalize messages and tools.

Proposed Python contract:

```python
ModelResponse(
    content=...,
    tool_calls=...,
    usage=...,
    model=...,
    provider_request_id=...,
    billed_cost=...,
    billed_currency=...,
    reasoning=...,
    provider_metadata=...,
)
```

Publish supported OpenAI-compatible message and tool codecs from a non-private module. The reasoning field should retain opaque replay data without making core depend on one provider's shape. `provider_metadata` needs a JSON-safe size limit and redaction path.

Compatibility: every new field is optional. Existing providers continue to construct the old minimum response. Usage-based estimated cost remains available when billed cost is absent, but the result labels it as an estimate.

Acceptance criteria:

- a fake provider round-trips reasoning details across two tool turns;
- billed and estimated cost remain distinguishable;
- the effective model and provider request ID reach the run result and trace;
- extension tests use only public codecs;
- secrets in provider metadata are removed by the logging policy;
- fixtures cover missing usage, missing price, non-USD currency, and malformed optional metadata.

### PR-04: Return a complete run execution envelope

Problem and evidence: the adapter parsed trace files to recover token totals and the spec hash, then read package and Git versions separately. Final verification success hid output-schema retries.

Proposed `RunResult` additions:

```text
run_id, session_id
requested_provider, requested_model
effective_provider, effective_model
schema_version, behavior_hash, execution_fingerprint
hiveloom_version
started_at, finished_at, duration_ms
usage, billed_cost
verification.first_pass_valid
verification.recovery_attempted
verification.attempts
verification.final_status
trace_path
```

The exact nesting should follow the new release's serialization conventions. The behavior matters more than these draft names: one JSON object should describe what was requested, what ran, what it cost, how it recovered, and where its durable evidence lives.

Compatibility: retain existing result keys through aliases or additive fields. Consumers should not need to parse human-readable messages. Missing provider data stays `null`, not a guessed value.

Acceptance criteria:

- clean first-pass, recovered success, verifier failure, guardrail halt, and runtime error have distinct fixtures;
- usage totals equal the sum of model calls without reading the trace;
- requested and effective model differ visibly in the wrong-model fixture;
- JSON serialization is stable across CLI and SDK entry points;
- a trace path never points into an automatically deleted directory.

### PR-05: Index recovered failures and other friction in the Hive

Problem and evidence: 24 recovered schema failures disappeared behind GLM's 30 final successes. Issue #31 asks the Hive to index friction events.

Proposed data model: add a normalized friction record keyed to the run, with category, phase, attempt, component, error fingerprint, recovered flag, timestamp, and a bounded summary. Initial categories should cover provider error, tool error, output validation failure, verifier failure, guardrail halt, retry, and loop limit.

Proposed query surface:

```text
hiveloom friction list HARNESS --category output_validation --recovered true --json
hiveloom stats HARNESS --include-friction --json
```

Compatibility: migrate existing Hive databases forward without rewriting historical trace files. Old runs simply have no indexed friction rows. Category values are versioned and unknown future values remain readable.

Acceptance criteria:

- every retry-producing event is indexed once;
- a recovered output validation failure remains queryable after final success;
- category, model, time range, component, and recovered status can be filtered;
- summaries respect redaction and size limits;
- migration and rollback tests use isolated Hive files;
- aggregate counts match trace fixtures for the same runs.

### PR-06: Add structured trace redaction and managed retention

Problem and evidence: the eval generated about 32 MB of traces containing CV evidence. The current regex-only configuration is easy to leave empty, and copied traces produced stale Hive paths after temporary directories were removed.

Proposed logging contract:

```yaml
logging:
  redact:
    keys: [email, phone, api_key]
    paths: ["tool.result.candidates[*].cv_text"]
    patterns: []
  retention:
    days: 30
    max_runs: 5000
    max_bytes: 1073741824
```

Redaction runs on structured values before trace serialization, Hive indexing, excerpt selection, and hook dispatch to logging consumers. Retention manages only files under a validated Hiveloom trace root. At-rest encryption remains outside this PR because it requires a key-management and recovery design.

Compatibility: no automatic deletion for harnesses without retention settings. Existing regex rules continue to work. A migration helper may translate old redaction lists into `patterns`.

Acceptance criteria:

- configured keys and paths never appear in raw trace bytes, Hive rows, or excerpts;
- invalid paths fail spec validation;
- pruning respects age, count, and bytes while keeping Hive references consistent;
- a dry run explains what would be pruned without deleting it;
- retention refuses paths outside the managed root and handles concurrent readers;
- tests use synthetic personal data and verify the original strings are absent.

### PR-07: Build bounded trace excerpts for evolution

Problem and evidence: issue #30 proposes feeding trace excerpts to the proposing model. Full matching traces are too large and contain private evidence. Friction rows provide a safer selection point.

Proposed behavior: build an incident packet around selected friction records. Each packet includes the event, a small number of adjacent model or tool events, hashes for omitted payloads, and explicit truncation markers. Selection has configurable byte and token budgets. Redaction runs before budgeting so hidden text cannot affect downstream output.

The analyzer receives structured packets rather than a raw trace string. Proposal metadata records run IDs, friction IDs, selection rules, and digests, which lets a reviewer inspect why a proposal was drafted without copying the full trace into the proposal.

Compatibility: evolution without excerpts behaves as it does today. Excerpts are opt-in until redaction and size controls have shipped.

Acceptance criteria:

- a selected retry includes enough preceding and following context to explain the failure;
- packet size never exceeds the configured hard limit;
- omitted content is labeled and hashed;
- redacted values remain absent from the model request fixture;
- missing or pruned trace files degrade to indexed summaries rather than failing the whole analysis;
- selection is deterministic for the same Hive state.

### PR-08: Trigger evolution proposals from repeated friction

Problem and evidence: issue #32 tracks the gap between final failure and recovered friction. A harness with 24 retries and 30 final successes still needs improvement.

Proposed configuration:

```yaml
evolution:
  auto_propose:
    enabled: true
    triggers:
      - kind: final_failure
      - kind: repeated_friction
        category: output_validation
        minimum_runs: 5
        window: 20
    cooldown_runs: 20
```

Triggers create proposals only. They never apply them. A deduplication key should include harness behavior hash, trigger, friction fingerprint, and evidence window so one repeated failure pattern does not flood the queue.

Compatibility: the current failure trigger maps to `kind: final_failure`. Existing configurations retain their behavior after migration.

Acceptance criteria:

- repeated recovered validation failures can produce one proposal;
- a single intermittent retry below the threshold produces none;
- cooldown and deduplication survive process restarts;
- a changed behavior hash permits a fresh evaluation of the same pattern;
- automatic proposals display `trigger=auto` and the exact evidence window;
- safety-frozen fields and explicit apply requirements remain unchanged.

### PR-09: Separate schema identity from execution identity

Problem and evidence: `version` has been used as both a spec-format marker and a runtime package version. The behavior hash needs to include referenced playbook prompt contents. A model override should identify a different execution without pretending the harness files changed.

Proposed identities:

- `schema_version`: the harness document format;
- `behavior_hash`: validated spec plus referenced behavior files, including prompts, hooks, extension declarations, skills, and output schemas;
- `execution_fingerprint`: behavior hash plus Hiveloom version, provider, effective model, runtime overrides, and other execution inputs that the new release can reproduce.

Add an atomic `hiveloom migrate HARNESS --json` command. Agents should never hand-edit the version field.

Compatibility: accept legacy `version` during a documented transition. Serialization uses the new field after migration. A loader must reject ambiguous documents containing conflicting old and new values.

Acceptance criteria:

- editing a referenced prompt changes the behavior hash;
- changing only a runtime model override changes the execution fingerprint but not the stored harness;
- identical harness copies produce the same behavior hash;
- migrate validates and rolls back on failure;
- old traces retain their original identifiers and remain queryable;
- `schema`, `explain`, package output, and docs use one definition for each identity.

### PR-10: Store numeric run metrics in the Hive

Problem and evidence: Recall@5, nDCG, longlist recall, hallucination rate, cost, latency, and stability could not be attached to the 199 runs already stored in the Hive.

Proposed type:

```python
RunMetric(
    run_id=...,
    name="recall_at_5",
    value=0.267,
    direction="maximize",
    unit="ratio",
    source="matching_eval_v1",
    scope="case",
    metadata={...},
)
```

Proposed CLI:

```text
hiveloom metrics record HARNESS --run-id ID --name recall_at_5 --value 0.4 --direction maximize --json
hiveloom metrics import HARNESS metrics.ndjson --json
hiveloom metrics list HARNESS --name recall_at_5 --json
```

Compatibility: keep `run_outcomes` for binary deferred feedback. Metrics are additive and do not reinterpret an old outcome. Names are user-defined, while direction, unit, and source make comparisons explicit.

Acceptance criteria:

- values must be finite numbers;
- import is transactional and reports invalid rows without partial writes;
- an idempotency key prevents duplicate metric ingestion;
- run, source, name, model, and time range are queryable;
- aggregate output always includes sample count and missing-value count;
- metrics can refer to case, run, or eval scope without mixing those scopes silently.

### PR-11: Add a scorer SDK and versioned eval spec

Problem and evidence: the external evaluator owns case loading, expected results, grounding checks, and metric calculation. Hiveloom cannot reproduce or validate that contract.

Proposed SDK: a scorer receives a case ID, case input, expected data, public `RunResult`, verification context, and optional artifacts. It returns zero or more `RunMetric` values plus structured diagnostics. Scorers run after Hiveloom verification so output validity and task quality remain separate.

Proposed eval document:

```yaml
schema_version: 1
harness: ../matching-harness
dataset:
  loader: matching_cases
scorers:
  - recall_at_k
  - ndcg
repetitions: 3
```

The exact construction commands should follow the post-release agent-native CLI conventions. Code-backed dataset loaders and scorers use the same trust boundary as other harness code.

Compatibility: this feature does not change normal `hiveloom run`. Version one can support local datasets and extension scorers without promising a hosted eval service.

Acceptance criteria:

- a fake dataset and scorer run without network or credentials;
- scorer exceptions are recorded separately from model failures;
- expected data never enters the model input unless the eval spec requests it;
- scorer outputs pass through metric validation and Hive ingestion;
- dataset and scorer digests contribute to eval identity;
- schema and catalog commands expose the eval contract to agents.

### PR-12: Add provider and model capability probes

Problem and evidence: 20 responses came from model families other than the requested one. Tool calling, structured output, reasoning replay, and provider alias behavior also vary by endpoint.

Proposed surface:

```text
hiveloom models probe HARNESS --model qwen3.5-9b --provider openrouter --json
```

The result reports requested and effective identity, accepted aliases, tool-call support, structured-output support, reasoning replay support, and whether each fact was declared or observed. A strict eval mode aborts before the case batch when identity does not match the configured alias policy.

Provider-specific routing remains in extensions. Core owns the generic capability result, enforcement modes, and provenance.

Compatibility: ordinary runs can default to the current behavior until a harness enables a policy such as `model_identity: exact`, `alias`, or `warn`. Eval specs should default to `exact` or an explicit alias set.

Acceptance criteria:

- wrong-model fixtures stop before any eval case executes in strict mode;
- documented aliases can pass without hiding the effective model;
- unsupported tool calling or structured output produces a machine-readable result;
- probe I/O and possible provider cost are explained before execution;
- declared and live-observed capabilities remain distinguishable;
- a cached probe expires when provider, requested model, or adapter version changes.

### PR-13: Add a resumable native eval runner

Problem and evidence: the adapter manages model matrices, case IDs, temporary directories, trace copying, retries, and partial results. An interrupted batch needs manual reconciliation.

Proposed CLI:

```text
hiveloom eval run eval.yaml --model MODEL --repetitions 3 --json
hiveloom eval resume EVAL_RUN_ID --json
hiveloom eval status EVAL_RUN_ID --json
```

The runner writes an atomic manifest with eval identity, case identity, repetition, requested and effective model, execution fingerprint, run ID, status, and metric-ingestion state. Resume schedules only missing cells. Infrastructure retries remain separate from model or verification outcomes.

Compatibility: the runner composes the public run and scorer APIs. It should not create a second execution path with different guardrails or logging behavior.

Acceptance criteria:

- terminating a fake batch halfway and resuming it produces one result per cell;
- completed cells are not billed or scored twice;
- a changed eval, harness, dataset, or scorer digest blocks unsafe resume;
- concurrency limits are deterministic and provider errors are recorded per cell;
- each cell has a durable trace or an explicit trace-disabled state;
- status and final output are valid JSON even while work remains incomplete.

### PR-14: Add eval reports and paired comparisons

Problem and evidence: the matching report had to join external metrics with token, latency, cost, validation, and stability data. Cost differences of 14.6 and 40.5 times mattered as much as small quality differences.

Proposed CLI:

```text
hiveloom eval report EVAL_RUN_ID --format json
hiveloom eval compare BASELINE_ID CANDIDATE_ID --format markdown
```

Reports include sample count, missing count, first-pass rate, recovery rate, final success, metric aggregates, latency, billed or estimated cost, and stability when repetitions exist. Comparisons pair matching case and repetition IDs before calculating deltas. Reports label unmatched cases and do not turn a small sample into a general model verdict.

Compatibility: JSON is the canonical report. Markdown is a rendering of the same data. Custom metrics remain visible even when no built-in formatter knows their domain meaning.

Acceptance criteria:

- a synthetic fixture reproduces clean, recovered, and failed counts;
- paired comparison excludes or labels unmatched cells;
- every aggregate reports `n` and missing values;
- billed and estimated cost are separated;
- stability is omitted when repetitions are insufficient;
- the JSON report can be regenerated from Hive data without reading raw traces.

### PR-15: Enforce structured sequential steps

Problem and evidence: the matching workflow required read, search-and-verify, and answer phases. Provider code enforced tool availability because string steps could not.

Proposed schema:

```yaml
loop:
  policy: sequential_steps
  steps:
    - id: read
      instruction: Read the deal.
      tools: [read_deal]
      require_tool_calls: [read_deal]
      max_model_calls: 2
    - id: search
      instruction: Find and verify candidates.
      tools: [search_and_verify_candidates]
      require_tool_calls: [search_and_verify_candidates]
    - id: answer
      instruction: Produce the final answer.
      tools: []
```

The loop owns step transitions and filtered tool registries. A step cannot complete until its required calls or explicit completion condition succeeds. Trace events mark step start, attempts, violations, and completion.

Compatibility: legacy string steps expand to instruction-only objects with current behavior. Enforced fields are opt-in. The migration command can normalize old steps without changing behavior.

Acceptance criteria:

- a tool hidden from a step cannot be called through the model response path;
- a missing required call causes the configured retry or step failure;
- per-step model and tool call limits stop the loop deterministically;
- the three-phase matching fixture produces three model turns and the expected tool sequence;
- dry-run output explains effective tools and limits per step;
- step events reach `RunResult`, traces, and the Hive without provider-specific code.

### PR-16: Add verification context and grounded-reference validation

Problem and evidence: three schema-valid outputs named talent IDs absent from tool evidence. The verifier saw the final output but lacked a public, structured view of supporting calls and artifacts.

Proposed API: pass a read-only `VerificationContext` containing run identity, public tool-call records, redacted tool results, step records, and declared artifacts. Verifiers request only the context parts they need.

Proposed built-in validator:

```yaml
verification:
  validators:
    - type: grounded_references
      output_path: "$.selected[*].talent_id"
      evidence_paths:
        - tool: search_and_verify_candidates
          path: "$.candidates[*].talent_id"
      normalize: string
```

Compatibility: existing validators keep their current call shape through an adapter or optional context parameter. Tool evidence remains subject to redaction and size limits.

Acceptance criteria:

- every selected reference present in evidence passes;
- an unseen reference fails even when the JSON schema passes;
- duplicate, numeric, string, missing, and null identifiers have defined behavior;
- evidence from unapproved tools or prior runs cannot satisfy the validator;
- failure output names the missing normalized values without exposing full private records;
- custom validators can use the context without parsing trace files.

### PR-17: Let evolution reason over metric objectives

Problem and evidence: one model was cheaper and better on Recall@5 in this eval, while another recovered more slowly and cost 40.5 times more. A binary outcome cannot express that tradeoff.

Proposed configuration:

```yaml
evolution:
  objectives:
    - metric: recall_at_5
      direction: maximize
    - metric: hallucination_rate
      direction: minimize
      ceiling: 0
    - metric: billed_cost_usd
      direction: minimize
```

Evolution analysis receives aggregate and paired metric history with sample counts, missing counts, execution fingerprints, and bounded friction excerpts. A proposal records the evidence window and states which objective it expects to change. It must not claim causation from an unpaired or small run set.

Compatibility: harnesses without objectives keep binary outcome analysis. Existing frozen safety fields remain frozen. Metric-triggered automation drafts proposals but never applies them.

Acceptance criteria:

- objective schema validates direction, units, floors, and ceilings;
- mixed execution fingerprints are rejected or grouped explicitly;
- proposals cite metric names, sample counts, baseline values, and evidence runs;
- missing metrics do not become zero;
- cost reduction cannot compensate for a hard hallucination ceiling violation;
- fake-provider tests prove the proposal request contains aggregates and excerpts, not raw private traces.

### PR-18: Teach generation and evolution the new workflow patterns

Problem and evidence: once the contracts exist, generated harnesses still need guidance on when to use structured steps, deterministic composite tools, grounding checks, and metric objectives. Otherwise the features remain manual and evolution may propose prompt changes for a control-flow problem.

Proposed changes:

- update generator and evolver contracts with the new schema and safety rules;
- teach the generator to prefer a composite deterministic tool when multiple upstream calls form one domain operation with an invariant between them;
- distinguish prompt failures from missing grounding, step-policy, provider, and metric instrumentation;
- add a small domain-neutral retrieval example with ranked metrics and grounded IDs;
- expose all new entries through schema, catalog, explain, and guide output.

Compatibility: guidance follows released capabilities and cannot land before them. Existing examples remain valid.

Acceptance criteria:

- generated examples validate without credentials or network;
- snapshot tests reject invented catalog entries and unsafe evolution fields;
- an evolution fixture recommends a grounding validator for unseen references rather than only rewriting the prompt;
- a workflow fixture uses structured step constraints instead of provider-side phase filtering;
- docs state when a deterministic composite tool is inappropriate;
- the example contains synthetic records and no matching-eval data.

## Suggested merge order

The dependency table permits some parallel work, but the review order should stay conservative after a large release.

1. Merge the rebase-audit documentation update.
2. Stabilize identity and public execution contracts with PR-09, PR-02, PR-03, and PR-04.
3. Add observability and privacy with PR-05 and PR-06.
4. Complete the existing evolution issues with PR-01, PR-07, and PR-08.
5. Add metrics and eval foundations with PR-10 through PR-14.
6. Add enforceable workflows and grounding with PR-15 and PR-16.
7. Connect metrics to evolution with PR-17, then update generation and guidance in PR-18.

PR-09 may need to split into a schema-and-loader PR followed by a hash-and-provenance PR. PR-11, PR-13, PR-14, PR-15, and PR-17 should also be split if their schema review becomes entangled with runtime behavior. The PR IDs can become umbrella issue labels in that case; they should not become large merge units.

## Work intentionally left out

- At-rest trace encryption: key storage, rotation, recovery, and multi-process access need a separate threat model.
- Provider-specific routing in core: extensions should implement OpenRouter or vendor policy against the generic provider contract.
- Talent-matching scorers in core: recall and nDCG may be reusable built-ins, but deal and candidate semantics belong in the eval package.
- Automatic proposal application: the matching evidence supports better proposals, not removal of human approval.
- Hosted eval scheduling or a remote control plane: local, resumable evaluation is enough to validate the contracts first.
- Automatic deletion without an explicit retention policy: existing users may rely on historical traces.

## Decisions to revisit after the release

The audit should answer these with code and migration fixtures:

1. Does the new release already have a canonical envelope or fingerprint type that should absorb PR-04 and PR-09?
2. Can friction be derived from a versioned event index, or does it need its own table?
3. Does the released verification API already expose structured tool evidence?
4. Should numeric metrics share storage with deferred outcomes or remain separate records joined by run ID?
5. Which provider metadata can be safely standardized without freezing one vendor's response shape?
6. Should retention live in each harness, in `HIVELOOM_HOME`, or in both with a documented precedence rule?
7. Can the eval runner reuse a released queue or session abstraction rather than add another manifest format?
8. Which CLI names match the released command hierarchy?

Record each answer in this file before opening the affected implementation PR.

## Per-PR review checklist

Every future PR in this queue should include:

- the matching-eval gap it removes;
- the post-release API or schema inspected before implementation;
- compatibility behavior for old harnesses, traces, proposals, and Hive databases;
- `--json` success and error fixtures;
- credential-free tests with isolated `HIVELOOM_HOME`;
- migration and rollback tests for every mutating command;
- docs, schema, explain, catalog, guide, and changelog updates where applicable;
- a note confirming that no private dataset row, CV content, provider key, or raw trace entered the repository.

The document itself should change when the evidence changes. The aggregate receipts above are the baseline captured before the large release.
