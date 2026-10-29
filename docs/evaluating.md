# Local evaluations

Hiveloom eval documents keep dataset and scoring policy outside
`harness.yaml`. A scorer runs only after normal harness verification and sees
the public `RunResult`; its numeric outputs use the same `RunMetric` validation
and Hive ingestion as `hiveloom metrics import`.

## Version one document

```yaml
schema_version: 1
harness: ../matching-harness
extensions:
  - eval_extension.py
dataset:
  loader: matching_cases
  params:
    split: test
  include_expected_in_input: false
scorers:
  - recall_at_k
  - name: ndcg
    params:
      k: 5
repetitions: 3
model_identity: exact
# model_aliases: [canonical-model-id]   # bare model ids, not provider/model
```

Inspect the schema without loading extensions, then validate the complete
local contract:

```bash
hiveloom eval schema --json
hiveloom catalog datasets --json
hiveloom catalog scorers --json
hiveloom eval validate eval.yaml --json
```

Validation resolves the harness, runs the dataset loader, constructs each
scorer, and returns only aggregate receipts: case count and content digests.
It does not print cases or expected values. Dataset loaders and scorers are
code, so eval-local extensions use the same trust gate as harness extensions.

`include_expected_in_input` defaults to false. In that mode the executor sees
only `EvalCase.input`; `expected` remains available to post-verification
scorers. Turning the flag on is an explicit request to add a delimited JSON
copy of expected data to the model input.

## Dataset and scorer extensions

An installed pack or trusted local extension can register both components:

```python
from hiveloom import EvalCase, RunMetric, ScorerOutput


class Cases:
    def load(self):
        return [
            EvalCase(id="case-1", input="Rank the records.", expected={"ids": ["r2"]})
        ]


class RecallAtOne:
    def score(self, context):
        matched = context.expected["ids"][0] in context.run_result.output
        return ScorerOutput(
            metrics=[
                RunMetric(
                    run_id=context.run_result.run_id,
                    name="recall_at_1",
                    value=float(matched),
                    direction="maximize",
                    unit="ratio",
                    source="retrieval_eval_v1",
                    scope="case",
                )
            ]
        )


def hiveloom_extension(hive):
    hive.register_dataset(
        "matching_cases",
        lambda params, ctx: Cases(),
        description="Load local synthetic or private matching cases.",
    )
    hive.register_scorer(
        "recall_at_k",
        lambda params, ctx: RecallAtOne(),
        description="Measure retrieved expected identifiers.",
    )
```

A loader returns `EvalCase` objects or matching dictionaries with unique IDs.
A scorer receives `ScorerContext`: case ID and input, held-out expected data,
case metadata, the public run result, optional verification context, and public
artifacts. It returns `ScorerOutput`, one `RunMetric`, or a list of metrics.

Scorer exceptions do not rewrite the model result. `ScoringResult.run_status`
keeps the harness outcome while each scorer gets its own success/error receipt.
Valid metrics from other scorers can still be ingested.

## Run and resume a batch

The native runner executes the case and repetition matrix through the normal
harness runtime, writes each trace below a durable Hiveloom-managed directory,
and checkpoints one atomic manifest:

```bash
hiveloom eval run eval.yaml --model qwen3.5-9b --repetitions 3 \
  --concurrency 2 --json
hiveloom eval status eval_0123456789abcdef --json
hiveloom eval resume eval_0123456789abcdef --json
```

`eval run` performs a live provider probe before scheduling cases. That probe
can make up to two provider calls and may incur cost. Eval documents default
to `model_identity: exact`; use `alias` only with an explicit `model_aliases`
set. A rejected identity or missing required capability stops the batch before
the first case runs.

`exact` means the provider must serve the model id the harness requested, which
providers that resolve an alias to a dated snapshot never do — asking Anthropic
for `claude-haiku-4-5` gets `claude-haiku-4-5-20251001` back, and `exact`
rejects that before the first case. Pin the served id instead:

```yaml
model_identity: alias
model_aliases: [claude-haiku-4-5-20251001]
```

Aliases are matched against the **bare model id the provider reports**, not
`provider/model`. Use `model_identity: warn` to record a difference on the
manifest without failing the batch.

The manifest records eval, dataset, scorer, and harness identities plus every
cell's case digest, repetition, requested and effective model, per-call model
identity (`call_models`), execution fingerprint, run ID, trace state, scorer
state, and metric-ingestion state.
Raw case IDs and expected values are not stored in the manifest. Resume refuses
to mix work after the eval, dataset, scorer, harness behavior, provider adapter,
or effective model changes. Completed cells are neither executed nor scored
again.

`--infrastructure-retries` applies only when the runner cannot obtain a
completed model result. Each retry has a distinct run ID. Harness verification
failures, guardrail halts, and provider responses recorded as run outcomes are
completed cells and are not reclassified as infrastructure errors.

Resume keeps that lifetime retry ceiling by default. After fixing an outage,
explicitly raise it to permit further attempts, using the CLI or SDK:

```bash
hiveloom eval resume eval_0123456789abcdef --infrastructure-retries 3 --json
```

```python
resume_eval(eval_run_id, execute_cell=callback, infrastructure_retries=3)
```

The value is an absolute ceiling of zero to twenty retries per cell, in addition
to the original attempt. A ceiling of three permits four total attempts; it
does not grant three new attempts on every resume. It cannot decrease the
recorded ceiling. Attempt counts and run IDs are retained, completed cells are
untouched, and result checkpoints still recover without another execution.
The new ceiling is persisted only after all resume identity checks pass.

The SDK's `run_eval(..., execute_cell=callback)` accepts a complete `RunResult`
from each callback. Before scoring, the runner atomically checkpoints that
result under `~/.hiveloom/evals/<eval_run_id>/results/` (or the configured
`HIVELOOM_HOME`). This includes artifacts added after the run's trace was
written, such as a workspace archive copied out of a disposable container.
The files have owner-only permissions and use the harness's `logging.redact`
patterns, keys, and paths, just like trace payloads. The manifest and Hive
index continue to contain receipts rather than model or tool payloads.

`resume_eval(..., execute_cell=callback)` scores a checkpointed result without
executing the cell again, including runs with tracing disabled. Scorers must
therefore accept redacted values during recovery. Artifact references are
preserved; the callback remains responsible for keeping their files available.
A malformed or unreadable checkpoint records an `InvalidCheckpoint` recovery
error and leaves the eval incomplete, without executing the cell again. Restore
a valid checkpoint from backup before resuming that cell. Runs without a result checkpoint,
including older evals and interruptions before a callback returns, retain
trace-based recovery. Keep the eval directory with the traces when moving or
backing up a run; older HiveLoom releases do not read result checkpoints.
Ctrl-C, `SIGTERM` or `SIGHUP` during `eval run` or `eval resume` stops the
batch: cells that had not started stay `pending`, the cells in flight return to
`pending` with `error_phase: interrupted` and no attempt charged against the
retry ceiling, the manifest is `incomplete`, and the command exits with status
130 after printing the `eval resume` command. The interrupted attempt keeps its
run ID, so a trace that finished anyway is recovered on resume instead of being
run again; otherwise the cell runs under the next run ID. The process leaves
without waiting for the provider calls in flight, which closes their sockets;
whether the server stops generating is up to the server. The first signal wins:
later ones are ignored while the manifest is written. A signal the caller
ignored on purpose stays ignored, so `nohup hiveloom eval run ...` still
outlives its terminal. An interrupt before the manifest exists (during the
probe or the trust prompt) leaves nothing to resume and says so.

## Report and compare

Eval manifests are indexed into the Hive as they change. Reports therefore use
indexed cell receipts and metrics and do not reopen raw traces:

```bash
hiveloom eval report EVAL_RUN_ID --format json
hiveloom eval compare BASELINE_ID CANDIDATE_ID --format markdown
```

JSON is the canonical format. Markdown renders the same calculations. Every
aggregate includes its sample and missing-value counts. Costs are grouped by
`billed`, `estimated`, or `mixed`; a comparison never treats billed and
estimated values as a paired cost delta.

A report names the model behind every cell. `effective_models` is the set of
models any cell reported; `cell_identities` has one entry per cell with its
`effective_model` (the last call that reported one), `call_effective_models`
(every provider call in order, `null` for a call that reported no model), and
`call_count`.
Both of the last two are `null` when the executor did not report per-call
identity (an older trace or a custom executor) and `[]`/`0` when the cell made
no calls. A cell with a run outcome that cannot say which model served it is
listed in `cell_identity_missing` with a `reason` of `no_model_calls`,
`call_without_effective_model`, or `no_effective_model`, and counted in
`cells.identity_missing_count`; `cells.identity_mixed_count` counts cells whose
calls served more than one model. Pending and infrastructure-error cells are
already in `cells.missing_count` and are not flagged. A collector should refuse
an eval with `identity_missing_count > 0` or with a model it did not expect in
`cell_identities`, rather than read a modelless cell as correct. A manifest
that recorded `call_models` cannot be resumed by an older release.

Comparisons pair the hashed case identity and repetition before calculating
deltas. Baseline-only and candidate-only cells remain listed instead of being
silently dropped. With multiple repetitions, each numeric metric can also
report mean within-case population standard deviation. Stability is omitted
when no case has at least two observations. Domain-specific stability metrics,
such as Jaccard agreement, remain ordinary scorer outputs.

## Identity and privacy

Every validated eval has four receipts:

- `spec_digest` covers the canonical eval document;
- `dataset_digest` covers the loader implementation and validated cases;
- `scorer_digest` covers scorer references and implementations;
- `eval_id` combines the three.

Only digests and counts are returned by validation. The raw case set and
expected values stay in the evaluator process. Scorers should put aggregates,
bounded diagnostics, and non-sensitive identifiers in their outputs, never CV
text, application rows, or full model/tool payloads.

Normal `hiveloom run` behavior is unchanged.

The checked-in [`ranked-retrieval`](../harnesses/ranked-retrieval/) example is
an offline reference for the complete local contract: synthetic cases,
structured retrieval and answer phases, grounded IDs, Recall@3, nDCG@3, and a
hard hallucination objective. `hiveloom eval validate eval.yaml --approve
--json` validates it without model credentials or network access.
