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
# model_aliases: [provider/canonical-model]
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

The manifest records eval, dataset, scorer, and harness identities plus every
cell's case digest, repetition, requested and effective model, execution
fingerprint, run ID, trace state, scorer state, and metric-ingestion state.
Raw case IDs and expected values are not stored in the manifest. Resume refuses
to mix work after the eval, dataset, scorer, harness behavior, provider adapter,
or effective model changes. Completed cells are neither executed nor scored
again.

`--infrastructure-retries` applies only when the runner cannot obtain a
completed model result. Each retry has a distinct run ID. Harness verification
failures, guardrail halts, and provider responses recorded as run outcomes are
completed cells and are not reclassified as infrastructure errors.

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
