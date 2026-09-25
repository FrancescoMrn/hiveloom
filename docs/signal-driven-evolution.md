# Signal-driven evolution

Evolution changes a harness because its runs said something. This document
covers how hiveloom finds *where* the runs point, makes every change a
prediction about that place, checks the prediction against the runs that
follow, and keeps only the changes that held. It also covers the memory
features that let a harness learn from runs without growing a prompt nobody
reviews.

```
runs ──► Hive ──► hiveloom signal ──► evolve (aimed proposal) ──► apply
  ▲                  (free)                 │                        │
  │                                         ▼                        ▼
  └──────────── hiveloom assess ◄── runs of the new version ◄── new version hash
                    (free)          (or: evolve --experiment eval.yaml --yes)
```

Everything on the left and bottom is counting and costs nothing. The only paid
step is the proposal itself, and it is aimed before it is written.

[`harnesses/signal-lab`](../harnesses/signal-lab) walks the whole loop offline,
with no API key: a located tool error, a reflected lesson, a refuted change
reverted and a confirmed one kept.

## 1. Locate: `hiveloom signal`

```bash
hiveloom signal ./h            # table view
hiveloom signal ./h --json     # the SignalMap evolution receives
```

Every ingested run is indexed with **features**: yes/no facts about what it did
or met on the way, never how it ended.

| family | example | from |
|---|---|---|
| `tool` / `tool_error` | `tool_error:http_get` | tool calls and error results |
| `spilled` | `spilled:read_log` | a result too large to inline |
| `step` / `step_violation` | `step_violation:read` | `sequential_steps` records |
| `playbook` | `playbook:triage` | playbook switches that succeeded |
| `memory` | `memory:iso-dates`, `memory:proposed` | relevance selection, `propose_memory` |
| `notes` | `notes:written` | the `notes` tool |
| `delegation` | `delegation:peer:success` | delegated children and referrals |
| `model` | `model:gpt-5-mini` | the effective executor |
| `input` | `input:long` | task size (short < 400, medium < 2000 chars, long) |
| `friction` | `friction:tool_error@http_get` | indexed friction, per category and component |

Friction that restates the ending (`loop_limit`, `guardrail_halt`,
`output_validation`, `verifier_failure`) is left out on purpose: as features
they would "explain" failure by definition. Those endings stay visible as
failure clusters and as `status:<status>` targets.

For one harness version, the signal map then reports:

- **Signals.** Each feature's failure rate with and without it, a two-sided
  Fisher exact p-value, and a Benjamini-Hochberg q-value across all features
  tested. `risk` means more common in failures. `protective` means more common
  in successes: a tool the good runs call and the bad runs skip is found just
  as readily as an error. Features that mark exactly the same runs are one
  signal. The most specific name is shown, and the others are listed as
  `aliases`. Each signal names its **levers** (the spec paths that could
  change it) and whether evolution may pull them (`addressable`).
- **Failure features.** What failed runs share, by prevalence. When there is
  nothing to contrast (every run failed), prevalence is the only evidence left.
- **Mechanisms.** Friction counted per `(category, component)`: events, runs,
  failed runs, recovered events. A change aimed at a mechanism is judged by
  whether its count falls. Small samples can show that long before they can
  show a success-rate change.
- **Loss classes.** Each failed run is charged to one of `provider`,
  `guardrail`, `limits`, `process`, `tooling`, `content` or `other`. The first
  four have a harness-level cause. `content` means the run finished and was
  wrong, which no plumbing change reaches. The split bounds what any harness
  change can buy.
- **Quality.** The success rate with its 95% Wilson interval, the smallest
  change this many runs could detect, and the runs per version needed to
  detect ten points. An underpowered population is reported as underpowered,
  not mined for patterns.
- **Verdict.**
  - `actionable`: a signal survives correction (q ≤ 0.1).
  - `suggestive`: p ≤ 0.1 but it does not survive correction.
  - `diffuse`: nothing separates failures, so look at content.
  - `underpowered`: fewer than two failing or passing runs.
  - `no_failures` and `no_runs`: as named.

An external failure label (`hiveloom outcome <run> failure`) counts as a
failure. A run that passed its validators and was wrong anyway is exactly
what validators cannot see. Runs ingested before feature indexing are counted
as `unindexed_runs`; re-ingesting their traces includes them.

## 2. Aim: every proposal names a target

`hiveloom evolve` puts the signal map into the proposing prompt as its own
section, ahead of the raw report. The proposal must carry a `target`:

```json
"target": {"signal": "tool_error:http_get", "expect": "decrease", "by": 0.4,
           "rationale": "in 7/9 failures vs 1/11 successes"}
```

- `signal` is one of the map's `targets` (a feature or alias, a mechanism, a
  `status:<status>`, `success_rate`) or `metric:<objective>` for a configured
  objective.
- `expect` is the direction. `by` is optional: the predicted absolute change,
  as a fraction of runs.

A missing or unknown target is sent back as repair feedback within the
existing three-call budget, like malformed JSON. A proposal that states
`objective_expectations` has already made a checkable prediction, so it needs
no separate target. The prediction is stored with the evolution record and is
what the assessment checks.

## 3. Assess: `hiveloom assess`

```bash
hiveloom assess ./h [--min-runs 5] [--json]
```

For every applied evolution, the old and new version's runs are compared on
the targeted signal, with the success rate as a guard:

- **Paired** when both versions ran the same eval cases (same eval, case and
  repetition): McNemar's exact test for rates, the sign test for metrics.
  Discordant pairs settle questions that two noisy rates cannot.
- **Unpaired** otherwise: Fisher's exact test for rates, Welch's z for metric
  means.

Verdicts:

| verdict | meaning |
|---|---|
| `pending` | fewer than `--min-runs` runs of the new version |
| `confirmed` | the target moved as predicted (p < 0.05) and success did not fall |
| `regressed` | the success rate fell significantly, whatever the target did |
| `refuted` | the target moved the other way, or a stated `by` was detectable at this sample size and did not appear |
| `inconclusive` | no significant change; reports the runs per version that would settle it |

An evolution recorded without a prediction (for example, one applied before
1.2.0) is judged on the success rate alone.

Assessments are also evolution's **attempt history**. Each applied evolution
carries its verdict, or the keep/revert decision an experiment took, so the
proposer does not re-propose a refuted change unchanged. Rejected proposals
still appear, unmeasured.

## 4. Experiment: `evolve --experiment`

```bash
hiveloom evolve ./h --experiment eval.yaml --yes [--rounds 3] \
    [--keep-inconclusive] [--remeasure-baseline]
```

Each round does the following:

1. Measures the current version on the eval. It does this once, or every
   round with `--remeasure-baseline`, so a lucky first draw is not the
   permanent bar.
2. Locates the signal and asks for one aimed proposal.
3. Applies it. YAML only: code changes are never applied in this mode.
4. Runs the same eval on the new version.
5. Assesses it pair by pair.
6. Keeps a `confirmed` change and reverts the rest. A revert restores the
   exact bytes and records the reverse evolution. `--keep-inconclusive` keeps
   a change the eval could not decide on.

Decisions are stored on the evolution row, and the next round's attempt
history carries them. The eval must run the harness being changed. `--yes`
is required because the loop applies and reverts on its own. Every gate,
frozen path, validation and rollback still applies. Each round costs up to
three strong-model calls plus one or two eval runs.

Eval size matters. Pairs are what make a small eval decisive: six improved
pairs and no worsened ones gives p = 0.03. Six cases that all changed is
the smallest sample that can confirm anything at all.

## 5. Memory that learns and scales

### Relevance-selected memory

```bash
hiveloom set memory.selection relevant --dir ./h
hiveloom set memory.max_selected 8 --dir ./h
```

With `selection: relevant`, a run is shown its pinned entries plus the entries
that best match its task, up to `max_selected`. Ranking is tf-idf over title
and content, with title matches weighted up, plurals folded, and ties broken
by declaration order. It is deterministic: the same task always gets the same
section, and the choice is made once per run, so every turn after the first
still hits the prompt cache.

The section says how many more lessons are stored. `search_memory`, registered
only in this mode, looks them up read-only. Mark a rule every run needs with
`pinned: true`. The selection is journaled as `memory_selected` and indexed as
`memory:<id>` features, so the signal map shows a lesson whose presence goes
with failure. `selection` and `max_selected` are frozen from evolution.

### Reflection

```yaml
evolution:
  reflect: {enabled: true, on: failure, max_lessons: 1, cooldown_minutes: 30}
```

After a run (a failed one by default), a strong model reads the run's task,
outcome, verifier feedback and friction next to the lessons already held, and
drafts at most `max_lessons` new ones. It is told that no lesson is better
than a speculative or one-off one. Drafts are queued with
`trigger: reflect` through the same path as `propose_memory`:

- redacted;
- deduplicated on content;
- checked against the budgets;
- applied only by `proposals apply`.

Eval runs never reflect, and the cooldown bounds spend: a reflection that
queues nothing still starts it. `evolution.reflect` is frozen from evolution.

### Compaction that keeps what it learned

Summarize-compaction now carries the previous summary forward. It is lifted
out of the transcript into `<previous-summary>`, with an instruction to keep
every identifier, value and ruled-out approach still true. The agent's newest
statement is added as a `<recent-state>` anchor, so the summary does not
describe a state the run has already left.

## Privacy

The signal map holds feature names, counts and statistics, never tool inputs,
outputs or model text. Successful runs appear in the evolution report with
their task and output only under `evolution.trace_excerpts.enabled`, the same
frozen opt-in that governs failure excerpts. Without it, the report says only
that passing runs exist and what they cost. Reflection sends one run's capped,
redacted task and output to the configured strong model. It is opt-in and
frozen.
