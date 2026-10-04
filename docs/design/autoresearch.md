# Design: autoresearch — a research director that guides a harness's evolution

Status: **revision 3; M0–M3 implemented, M4 in part** (branch `autoresearch`,
2026-09-29). The user reference is [research.md](../research.md).

**Built:**
- The engine (`hiveloom.research`):
  - the charter, the ledger, and the budget pools, including `data`;
  - the cost and power plan, sequential looks, and the confirmation reserve;
  - every stop condition in §8, including the ceiling.
- Research-safe execution (`allow|sandbox|replay|deny`).
- The packaged director harness with the §13 tools.
- The CLI.
- The `research-lab` demo, in both modes.
- The M0 bake-off (`evals/research-bakeoff`).
- Concepts mode (M2 and M3):
  - the frame unit, the approval gate, and the examiner's sealed cases;
  - the lexical near-duplicate filter;
  - per-criterion scoring;
  - judge ensembles, trusted only on your labels, with audits each round;
  - typed questions under a budget;
  - evidence strength downgraded for unmeasured criteria;
  - the proxy-gap report;
  - a test showing that a director gaming its working cases is caught by the
    sealed confirmation.
- The workbench's Research tab (M4):
  - program control;
  - contract approval with sample cases;
  - question cards;
  - trust per criterion;
  - the hand-off to Improve.

**Departures:**
- Probes (unit 8) are not built.
- The dataset is frozen at approval. A mid-program `grow_dataset` is not
  built.
- A change can be kept as **`improved`**: the success rate rose significantly
  while the predicted target did not move. The bake-off found this case: a
  retried call still logs its first error. Futility therefore requires both
  the target and the success rate to be out of reach.
- "Addressable" is judged against the charter's levers.
- A run's pass or fail is written as its Hive outcome, from the measured
  criteria. `signal`, `assess` and the pairing therefore read contract
  success without a second notion of success.
- The bake-off uses four builds of a scripted planted-defect desk. The
  director received failing examples in its brief, and a typed change schema,
  after the first live run showed it theorizing blind. That fix was made on
  one of the four harnesses and is disclosed in the results.

**M0 decision, revised 2026-09-30: GO, as evolve's autonomous mode.**

The pre-registered question (whether a director makes **≥ 1.5×** the defects of
`evolve --experiment`) was the wrong question, and it was not met:
- research fixed 19/24 and 21/24 planted defects against evolve's 17/24 and 20/24;
- that is 1.12× and 1.05×;
- on this benchmark 1.5× could not have been reached at all.

After seeing those results, the product owner set the right question:

> Evolve is the deliberate, manual step. Autoresearch is that same step with an
> external director that runs it repeatedly, unattended. It does not have to beat
> evolve. It has to be **as good as a person running evolve, without the person**,
> and **safe while nobody watches**.

Against that, both runs pass:

| test | result |
|---|---|
| not worse | never fewer defects than evolve: +2, then +1 |
| safe unattended | 0 false promotions in 24 programs; the unfixable decoy never "fixed" |
| stops rather than spends | the ceiling stop, with a recommendation |
| cost | at parity per fixed defect |

The criterion changed after the data was seen, and this record says so. The 1.5×
bar is kept above as the original and superseded criterion. Consequences:
- the feature ships as `hiveloom evolve --research` and the workbench's
  "Evolve autonomously";
- `evolve --experiment` remains the deliberate loop;
- what the next benchmark must test is autonomy: long unattended runs that do
  not drift, overspend or stop late (`evals/research-bakeoff/scripts/autonomy.py`).

The M2 live check passed: live judges were trusted at κ 1.0 on 11 labels, within a
12-question budget.

**Pending:** a harder planted-defect benchmark, candidates listed in Versions, and M5
(critic, coaching). Cross-program findings are built.

- **Revision 2** recorded the product decisions (§17).
- **Revision 3** closes the gaps a feasibility review found:
  - evaluation validity and the user's approval gate (§6);
  - cost and statistical power (§7);
  - stopping and the ceiling (§8);
  - research-safe execution (§9);
  - holdout discipline (§10);
  - privacy (§11);
  - the human interaction model (§12);
  - failure modes (§14);
  - the architecture (§15);
  - a milestone plan that tests the premise before building on it (§16).

## 1. The idea

hiveloom separates the **executor** (a small model working inside a confined
harness) from the **evolver** (a strong model that proposes one gated change
from run evidence). Since 1.2.0 a proposal aims at a located signal, and
`assess` checks it against its prediction.

What is still missing is *research*:

- an agenda across rounds;
- competing hypotheses;
- cheap probes before expensive experiments;
- memory of what was learned;
- often, something to measure against at all. Users usually bring a handful of
  examples, or only the concepts they care about.

**autoresearch adds a second agent, the director.** It runs a research program
on a harness:

1. turns the user's concepts into an evaluation, which you approve and it
   keeps growing;
2. reads the evidence;
3. forms hypotheses and spends its budget on probes and experiments as it
   judges best;
4. reads the measured results and decides what to try next.

A deterministic **engine** owns the state, budget, measurements and every
verdict. The executor keeps doing its job inside the harness, unchanged.

```
 user: goal, concepts, a few examples ─► frame ─► [APPROVE evaluation contract] ─► dataset
                                                                                    │
   ┌──────────────────────── rounds (engine-owned, budgeted) ◄───────────────────────┘
   │  baseline ─► survey ─► hypothesize ─► probe ─► experiment ─► assess ─► interpret
   │                ▲             director (a confined hiveloom harness, typed tools)   │
   │                └──────── continue / pivot / grow_dataset / ask / stop ◄────────────┘
   ▼
 confirm on sealed examiner cases (once) ─► report ─► [APPROVE promotion] ─► live harness
```

## 2. What exists, and what is new

| Area | Reused from 1.2.0 | New |
|---|---|---|
| Measurement | eval runner, `signal`, `assess` (paired McNemar/sign), stats helpers | eval splits, sequential stopping rules, a cost model |
| Changes | aimed proposals, `gate()`, frozen paths, candidate specs via construct | candidate *copies* owned by a program; multi-change bundles |
| Probes | `fork --at` + `--resume` | automatic fork and slice probes |
| Scoring | validators; scorers from eval extensions (**no builtin scorer exists**) | a builtin rubric-judge scorer with measured trust |
| Data | eval datasets as extensions | program datasets built from concepts, versioned, with provenance |
| Agents | harness-as-agent precedent (the workbench copilot) | director, examiner, optional critic harnesses in the package |
| Safety | confinement, egress, trust, guardrails, the delegation child cost cap | a research execution policy; budget debits; holdout ledger |
| UI | workbench: copilot, Versions, Trace, Improve, live run control | Research tab, question cards, program control |

## 3. Roles

| Role | What it is | Can | Cannot |
|---|---|---|---|
| **Executor** | the harness's own model | run the task in a candidate version | know a program exists |
| **Director** | a strong model, itself a confined hiveloom harness shipped in the package (like the workbench copilot) | read briefs and redacted excerpts; propose criteria, cases, questions, hypotheses, probes, experiments; interpret rounds (§13) | edit files; run shell; use the network; write a verdict; see sealed cases; touch the live harness |
| **Examiner** | an independent model, a different identity from the director | write sealed confirmation cases from concepts, criteria and anchors | see candidates, hypotheses, results, or director-authored cases |
| **Judge** | the rubric scorer for non-mechanical criteria; may be an ensemble of distinct identities | score one output against one criterion | score before it has earned trust (§6.4) |
| **Critic** (optional) | an independent model | veto an unfalsifiable, underpowered or already-refuted hypothesis | approve anything; its silence is never a pass |
| **Engine** | deterministic Python, `hiveloom.research` | own state, budget, ledger, datasets, candidates, measurements, verdicts; refuse any call the state does not allow | call a model on its own behalf |
| **User** | the program's owner | set the charter; approve the evaluation contract; answer questions; label; promote | — |

The principles come from AutoResearch, sharpened:

- A deterministic engine owns the state machine; LLM roles only propose,
  through typed commands.
- **Whoever wants an outcome never produces the verdict.** The director
  proposes; the engine measures; the examiner writes the test the director
  cannot see; the user promotes.

## 4. The program: its own folder, many versions

A program produces many versions of one harness, so it lives beside the
harness, not inside `harness.yaml`. Like forks, it nests under the base
harness so the workbench shows it there.

```
my-harness/
  harness.yaml                          # live; changes only through an approved promotion
  .hiveloom/research/<program>/
    research.yaml                       # charter (user-owned, never reachable from evolution)
    concepts.md, seeds.jsonl            # the user's goal material
    contract/v<n>.yaml                  # evaluation contract: criteria, checks, thresholds (approved)
    dataset/v<n>/cases.jsonl            # immutable once used; provenance per case
    sealed/                             # examiner cases — engine-only; never in a director context
    candidates/<id>/                    # full candidate harness copies
    ledger.jsonl                        # hash-chained record of every action, debit and verdict
    questions.jsonl                     # follow-ups and answers
    state.json, report.md               # engine-written only
```

```yaml
# research.yaml — the charter
goal: >
  Answers about invoices are exact, cite the ledger row, never invent an
  amount; refunds keep their sign.
concepts: concepts.md
seed_examples: seeds.jsonl                 # optional
levers: [system_prompt, memory.entries, tools, loop.steps, playbooks]   # ⊆ evolution.mutable
guard: {success_rate: no_regression, cost_per_run: "+20%"}
budget:
  usd: 10.00
  wall_clock_minutes: 240
  split: {data: 0.2, exploration: 0.3, experiments: 0.35, confirmation: 0.15}   # §7.3
human:
  questions: 12                            # total follow-ups the program may ask
  batch: 4                                 # asked at most this many at a time
  approval: [contract, promotion]          # hard gates (§12)
execution:                                 # §9
  tools: {http_get: replay, shell: deny, file_write: sandbox}
  mcp: {tickets: {list_tickets: allow, get_ticket: allow, "*": deny}}
  max_cost_per_run: 0.05
models:
  director: openrouter/anthropic/claude-sonnet-5
  examiner: openrouter/openai/gpt-5-mini        # must differ from the director
  judge: [openrouter/openai/gpt-5-mini, openrouter/google/gemma-4-31b-it]
  critic: null
data:                                           # §11
  providers_allowed: [openrouter]
  redact: [...]                                 # extra patterns for prompts to research roles
```

## 5. The research loop (engine state machine)

Each state is one engine **unit**. `hiveloom research step` advances exactly
one unit and returns, so a program is resumable from files and never depends
on chat memory. States marked ⏸ pause for the user.

| # | Unit | Actor | Output | Leaves when |
|---|---|---|---|---|
| 1 | **frame** | director | draft evaluation contract: criteria, checks, thresholds; questions | contract drafted |
| 2 | **approve contract** ⏸ | user | approved `contract/v1` (or edits, and back to 1) | the user approves |
| 3 | **dataset** | director, examiner, user | `dataset/v1` (working cases), sealed cases, anchors; judge trust measured | enough cases per criterion for the planned tests (§7.1) |
| 4 | **plan** | engine | cost model; budget pools; detectable effect per criterion | always; the plan goes in the brief |
| 5 | **baseline** | engine | the incumbent measured on the working split | runs done |
| 6 | **survey** | engine → director | brief: signal map, loss classes, coverage, power, ledger, calibration, budget left | always |
| 7 | **hypothesize** | director (critic) | registered hypotheses | at least one accepted, or `interpret` |
| 8 | **probe** | engine | fork/slice probe outcomes | probes done, or the director drops the hypothesis |
| 9 | **experiment** | engine | candidate vs incumbent, paired, with sequential looks (§7.2) | stopping rule fires |
| 10 | **assess** | engine | verdict, guard check, calibration | always |
| 11 | **interpret** | director | findings, next focus, decision | continue / pivot / grow_dataset / ask / stop |
| 12 | **confirm** | engine | the final incumbent vs base on sealed cases, **once** (§10) | done |
| 13 | **report** | engine | `report.md`, evidence strength (§6.6), recommendation (§8) | always |
| 14 | **approve promotion** ⏸ | user | a proposal bundle applied to the live harness, or declined | the user decides |

The engine also leaves the loop on any stop condition in §8.

## 6. Evaluation validity (the core risk)

A program that optimizes a proxy the user never meant can report "confirmed"
and be wrong. Everything in this section exists to keep the measurement tied
to the user's intent.

### 6.1 The evaluation contract, approved before any money is spent on changes

`frame` turns the concepts into a contract the user reads and approves:

```yaml
criteria:
  - id: amount-exact
    says: "The reported amount equals the ledger amount"
    check: {kind: code, scorer: exact_field, field: amount}      # deterministic
    weight: 1
  - id: no-invention
    says: "Never reports an invoice id that is not in the ledger"
    check: {kind: validator, builtin: grounded_references, ...}
  - id: tone
    says: "Answers in one plain sentence"
    check: {kind: judge, rubric: "..."}                          # needs trust (§6.4)
goal_thresholds: {amount-exact: 0.95, no-invention: 1.0}         # "done" on the working split
sample_cases: 6                                                  # shown with the contract
```

The user approves the criteria **and a handful of sample cases with their
expected outcomes**. Approving abstractions alone is where misunderstandings
hide; seeing concrete cases is where they surface. Editing the contract later
creates `contract/v2`, re-measures the incumbent, and is ledgered. The
director can propose contract changes but never apply them.

### 6.2 Anchors: few labels, spent where they matter

Seeds and every answered question become **anchor cases** with a human label.
The user's labels are the scarcest resource, so the director is scored on how
it spends them:

- **Disagreement first.** Ask about outputs where judge ensemble members
  disagree, or where the judge and the nearest anchors conflict.
- **Coverage.** Ask about criteria with no anchors.
- **Boundary.** Ask about cases near a threshold ("is 1250.0 vs 1250 exact?").

The engine refuses a question that duplicates an answered one and enforces
`human.questions` and `human.batch`.

### 6.3 Cases: the director's and the examiner's, kept apart

- **Working cases** (`provenance: director`) are variations and edge cases per
  criterion. The director may grow them whenever coverage or power is short.
  Each case records the criterion it targets and the anchor it derives from.
- **Sealed cases** (`provenance: examiner`) are written by the examiner from
  the concepts, the approved contract and the anchors only. They are stored
  where the engine never reads them into a director context. The examiner is
  prompted to write **adversarially**: the cases a careless implementation of
  the concepts would get wrong.
- **Near-duplicate filtering.** An embedding-free lexical check (the same
  tf-idf as memory selection) rejects working cases that nearly copy a sealed
  case, so the working split cannot converge on the test by accident. The
  director is told only that a case was rejected, not why or which.

### 6.4 Judges must earn trust, and keep it

A judge scores a criterion only while its agreement with the user's anchor
labels holds:

- **Trust:** Cohen's κ ≥ 0.6 and agreement ≥ 0.85 on ≥ 8 anchors for that
  criterion (defaults; the charter may raise them). Below that the criterion is
  **unmeasured**. It cannot confirm or refute anything, and the director is
  steered to ask for labels instead.
- **Ensemble:** two or more judge identities; the engine records
  disagreements, never averages them away, and a split verdict counts as
  inconclusive for that case.
- **Audit sampling (drift).** Every round, the engine samples a few judged
  outputs from the *current incumbent* into the question queue. If the user's
  labels on them diverge from the judge's (κ falls below the floor), the
  criterion drops back to unmeasured mid-program. This is the tripwire for a
  director learning to please the judge instead of the user.

### 6.5 A proxy-gap report

The report shows, per criterion, how it was measured:

- deterministic check;
- a trusted judge, with its κ;
- unmeasured.

It also shows how many conclusions rest on each. Sealed-case results are
reported against working-case results, and a large gap is flagged as possible
overfitting to the working split.

### 6.6 Evidence strength, per promotion

| Label | Requires |
|---|---|
| `confirmed` | confirmed on the working split **and** on sealed cases, with every criterion that changed measured deterministically or by a trusted judge |
| `supported` | confirmed on the working split; sealed set inconclusive, or some criteria measured only by an untrusted judge |
| `provisional` | directional only, or anchors only |

Any label may be promoted; the label and its reasons travel with the bundle
into the harness's version history.

## 7. Cost and statistical power

### 7.1 Plan before spending

Once the incumbent is measured, the engine builds a **cost model**: mean and
p90 cost per run, per case, per criterion. It then tells the director, in the
brief, what the budget buys:

```
per experiment on 40 cases × 2 reps: ~$0.62 (p90 $0.95)
detectable change at that size: success ±0.21, mechanism share ±0.18
budget left for experiments: $3.50 → about 5 experiments at this size
```

A hypothesis whose predicted effect is below the detectable change is
allowed; the director owns that choice. It is flagged, and the engine suggests
`grow_dataset` or a mechanism target instead. **Mechanism targets are the
default recommendation**: counting a failure mode resolves at sample sizes
where a success rate cannot, which is the 1.2.0 signal lesson.

### 7.2 Sequential experiments that stay valid

An experiment is planned at its full size, then looked at in stages:

- **Stop for harm** at any look, as soon as the guard or success regresses
  significantly. Checking often for harm costs nothing statistically, and it
  is what saves money on bad candidates.
- **Stop for futility** when, even if every remaining pair went the
  candidate's way, the predicted effect could no longer be reached.
- **Declare benefit only at the planned size.** This avoids the inflated false
  positives of peeking.

With several criteria in play, benefit claims are Holm-corrected across the
criteria the hypothesis names.

### 7.3 Budget pools

The charter splits the budget into pools (`data`, `exploration`,
`experiments`, `confirmation`). The engine:

- debits every paid call to a pool before making it;
- refuses a unit that would overdraw its pool;
- **reserves the confirmation pool up front**, so a program can always afford
  its one sealed confirmation.

Unspent exploration flows to experiments. The director may move money between
pools only within ±25% of the charter's split.

## 8. Stopping, and the ceiling

A program stops, and says why, on the first of:

| Condition | Rule |
|---|---|
| goal reached | every `goal_thresholds` met on the working split → go to confirm |
| budget | a pool (other than confirmation) is exhausted |
| time | `wall_clock_minutes` elapsed |
| no progress | three consecutive rounds without a confirmed change |
| **ceiling** | loss attribution says the remaining failures are mostly `content` (the model finished and was wrong) **and** no remaining signal has an addressable lever |
| director stop | `interpret` returns `stop` with a reason |
| user stop | from the CLI or the workbench |

A ceiling stop ends with an **operator recommendation**, not silence:

- "68% of remaining failures are content errors no lever in this charter
  reaches."
- "Options: a stronger executor (model is frozen from evolution — your
  call); widen the levers to include `tools` so the harness can fetch the
  missing facts; narrow the task."

## 9. Research-safe execution

A program runs the executor hundreds of times on invented inputs. A harness
with real tools would do real things that many times. The engine therefore
runs every research run under an **execution policy**. It is installed like
the delegation child cost cap: an extra layer that can only tighten a run,
never widen it.

- **Classification.**
  - Builtin tools carry effect tags (`read`, `write`, `network`, `exec`) in
    the catalog.
  - Code tools and MCP tools have no trustworthy tags, so the charter must
    classify them.
  - An unclassified effectful tool blocks `init`, with the list of tools to
    declare.
- **Modes per tool:**

  | Mode | Behavior |
  |---|---|
  | `allow` | runs as declared |
  | `sandbox` | `file_write` into the candidate's scratch directory; shell under confinement with `network: false` |
  | `replay` | serves recorded results; see below |
  | `deny` | the tool is absent from candidates |

  `replay` serves results captured from the base harness's own real runs,
  keyed by tool name and normalized input. It is the tool-level counterpart of
  a fork. A replay miss is a tool error, so the program measures what it can
  actually replay.
- **Egress and confinement** of the harness stay in force. Research roles'
  prompts pass the program's `data.redact` patterns in addition to the
  harness's own.
- `max_cost_per_run` is an extra `max_cost_usd` guardrail on every executor
  run.

## 10. Holdout discipline

- Sealed cases are **confirmed against once** per program. A second
  confirmation needs a *fresh* sealed batch from the examiner, paid from the
  confirmation pool and ledgered as `sealed/v2`.
- Every read of `sealed/` by the engine is a ledger event. The report lists
  them, so a program that looked more than once cannot hide it.
- The director never receives sealed cases, their runs, excerpts or per-case
  outcomes; after confirmation it learns only the aggregate verdict and
  evidence strength.

## 11. Privacy and data handling

- **Providers.** Concepts, seeds and anchors go to the director, examiner and
  judges. The charter's `providers_allowed` lists which providers may receive
  them. A local provider (Ollama, vLLM) keeps everything on the machine.
- **Redaction.** Every prompt to a research role is redacted with the
  harness's `logging.redact` plus `data.redact`, before the call, like egress.
- **Retention.** Programs live under `.hiveloom/`, which the harness template
  already ignores in git and excludes from `hiveloom package`. `report.md`
  contains aggregates and case ids, not case text, unless exported with
  `--include-cases`.
- **Exports.** A promoted program's dataset can become the harness's
  regression eval (§17), but only by an explicit command, so private anchors
  never leave the program folder by accident.

## 12. The human in the loop

- **Two hard gates**, never skipped: approving the evaluation contract (with
  sample cases), and approving a promotion.
- **Questions are asynchronous and batched.** A program asks at most
  `human.batch` questions at a time and keeps working on what does not depend
  on them. Unanswered questions time out into "unknown", never into an
  assumed answer.
- **Question types:**
  - *disambiguate* a criterion;
  - *label* an output;
  - *confirm* a boundary;
  - *audit* a sampled judgment (§6.4).

  Each has a typed answer shape, so a UI card and a CLI prompt render the same
  thing.
- **Fatigue guard.** `human.questions` is a hard cap. The report says how the
  labels were spent and where more would help most.

## 13. Director tools (typed; everything else is refused)

| Tool | Kind | Effect |
|---|---|---|
| `brief()` | read | charter, plan, budget by pool, state, signal map, coverage, trust, ledger summary, calibration |
| `runs(filter)` / `excerpt(run_id, around)` / `diff(candidate)` | read | working-split evidence only |
| `propose_contract(contract)` | act | a draft for the user's approval |
| `add_cases(criterion, cases)` | act | working cases; near-duplicate filter applies |
| `ask(questions)` | act | within the question budget |
| `register_hypothesis(h)` | act | typed; refused with a reason if frozen, unknown, duplicate or refuted |
| `design_probe(h, kind, n)` / `design_experiment(h, changes, size)` | act | gated by `gate()`; cost-checked against the pools |
| `move_budget(from, to, usd)` | act | within ±25% of the charter's split |
| `interpret(handoff)` | act | ends a round |
| `note(text)` | act | a finding for the record |

How much to change at once is the director's choice. Bundles are allowed; the
engine records exactly what each candidate changed and fingerprints bundles
in the ledger.

## 14. Failure modes and mitigations

| Failure | How it shows | Mitigation |
|---|---|---|
| Optimizing the wrong proxy | confirmed gains the user rejects | approved contract with sample cases (§6.1); audit sampling (§6.4); proxy-gap report (§6.5) |
| Director grading its own homework | working-split gains that vanish on sealed cases | independent examiner; near-duplicate filter; evidence strength (§6.3, §6.6) |
| Judge drift, or a director that games the judge | judge and user labels diverge | trust gate + audit tripwire; ensemble disagreement → inconclusive |
| Chasing noise | a string of small "wins" | power plan; benefit only at planned size; Holm; calibration shown to the director |
| Holdout leakage | repeated confirmations | once per sealed batch; reads ledgered (§10) |
| Runaway cost | budget overrun | pool debits before calls; per-run cap; confirmation reserve |
| Real-world side effects | writes, calls, spend from research runs | execution policy; unclassified tools block `init` (§9) |
| Ceiling | rounds without progress on content errors | ceiling stop with an operator recommendation (§8) |
| Director loops | the same idea resubmitted | ledger fingerprints; refuted ideas refused unless new evidence is cited |
| User fatigue | questions unanswered | budget, batching, timeouts to "unknown" |
| Provider outage | calls fail | three-state outcomes; the program pauses, never fakes a result |
| Non-reproducible claims | a result no one can re-derive | a hash-chained ledger with model identities, dataset and spec hashes; verdicts recomputable from the ledger by the deterministic engine |

## 15. Architecture

```
hiveloom/research/
  charter.py        schema + validation of research.yaml (frozen from evolution)
  engine.py         the state machine; one unit per step; refusal reasons
  ledger.py         hash-chained events; debits; sealed-read events; verify
  budget.py         pools, debits, the cost model, reservations
  planning.py       power and cost plans; sequential stopping rules; Holm
  dataset.py        contracts, cases, provenance, versions, near-duplicate filter
  judges.py         the rubric-judge scorer; ensembles; κ and trust; audit sampling
  examiner.py       sealed case generation (a role harness call)
  execution.py      the research execution policy: classify, sandbox, replay, deny
  candidates.py     candidate copies; gate(); bundle fingerprints
  probes.py         fork and slice probes
  questions.py      typed questions and answers; batching; timeouts
  report.py         report.md; evidence strength; proxy gap; recommendations
  roles/director/, roles/examiner/, roles/critic/   shipped role harnesses (harness.yaml + typed tools)
```

- **CLI:**
  - `hiveloom research init|step|run|status|questions|answer|approve|report|stop`, all `--json`;
  - `hiveloom eval export --from-program` for the regression-eval hand-off.
- **Workbench:**
  - API routes mirror the CLI;
  - program events stream the way run events do;
  - a **Research tab** shows the charter, the contract with its approval
    button, the dataset by criterion with provenance and judge trust, the
    hypothesis board, a timeline, calibration, budget pools, and promotion
    into the Improve tab;
  - the **copilot** helps set a program up and relays the director's
    questions as cards in the conversation;
  - candidates appear in **Versions**, and probe forks in **Trace**.

## 16. Plan

Each milestone ends in something shippable and a test that proves it.

| Milestone | Scope | Exit criteria |
|---|---|---|
| **M0 — the premise test** (first, small) | engine, ledger, candidate copies, budget debits; a real LLM director with the typed tools; a **given** eval (no dataset building) | the bake-off below completes; go/no-go decision recorded |
| **M1 — safe and honest core** | research execution policy; cost model and power plan; sequential stopping; confirmation reserve; stop conditions incl. ceiling; CLI; offline demo `research-lab` with a scripted director | the demo runs offline in CI; unclassified effectful tools block `init`; the stop rules have unit tests |
| **M2 — evaluation from concepts** | frame, contract approval, anchors, questions, working cases, the rubric-judge scorer with trust and audit | on a planted-concept benchmark, contracts approved by a test user yield judges with κ ≥ 0.6 within the question budget |
| **M3 — independence** | examiner, sealed cases, near-duplicate filter, one-shot confirmation, evidence strength, proxy gap, report, promotion bundle | a benchmark where a director that games the working split is caught by the sealed confirmation |
| **M4 — workbench mode** | Research tab, question cards, program control, Versions/Trace integration | end-to-end program from the UI; question cards and CLI answers interchangeable |
| **M5 — optional** | critic; coaching (§18); cross-program findings | — |

### The M0 bake-off: does a director beat today's loop?

The question is whether an LLM director finds more real improvements per
dollar than `evolve --experiment`.

- **Benchmark.** Harnesses with **planted defects**: known, fixable faults of
  graded subtlety, injected into working harnesses. Examples: remove a
  memory rule, break a step's tool requirement, weaken an output instruction,
  add a distracting tool.
  - Ground truth is known, so "found and fixed" is countable.
  - It runs on the offline demos plus ranked-retrieval and an ARC subset.
- **Arms.** (A) `evolve --experiment`, (B) the director, both with the same
  budget, the same eval and three seeds.
- **Pre-registered metrics:**
  - planted defects recovered;
  - cost per recovered defect;
  - false promotions (a promoted change that regresses on fresh cases);
  - calibration error;
  - wall clock.
- **Go if** B recovers ≥ 1.5× A's defects at ≤ A's false-promotion rate. The
  estimated cost is under $50 on OpenRouter models.
- **No-go** means we keep the loop and its safety machinery (M1 has value on
  its own) and reconsider the director.

Rough effort, on this codebase:

| Milestone | Effort |
|---|---|
| M0 | 1–2 weeks |
| M1 | 2 weeks |
| M2 | 3 weeks |
| M3 | 2 weeks |
| M4 | 2–3 weeks |

The dominant risk is M2's evaluation quality, not the engineering.

## 17. Decisions taken

1. **The program is separate from the harness.** It has its own folder and
   charter, nested under the base harness.
2. **How much each experiment changes is the director's choice** within its
   budget; the engine records it.
3. and 4. **Held-out checks and promotability are data questions.** The
   director builds the evaluation from concepts and few examples, with
   budgeted follow-ups. An independent examiner writes sealed cases, and
   promotions carry an evidence-strength label.
5. **Coaching** is a later, optional feature.
6. **Home:** a CLI mode and, primarily, a workbench research mode beside the
   copilot.

## 18. Future features

- **Coaching.** The director steers a probe run live through `user_steer`
  ("would the executor succeed if told X?"). Coached runs are journalled as
  diagnostics and held out of every fitness bucket, like an off-spec model
  swap.
- **Cross-program findings.** Refuted ideas and confirmed findings carried
  between programs on the same harness.
- **Regression-eval export by default** for promoted programs, once §11's
  opt-in has proved itself.

## 19. Open questions, with proposed defaults

| # | Question | Proposed default |
|---|---|---|
| 1 | Can the examiner's sealed set grow mid-program? | No. One batch per program, a fresh batch per extra confirmation (§10). |
| 2 | Judge trust thresholds: fixed or charter? | Defaults κ ≥ 0.6, agreement ≥ 0.85, ≥ 8 anchors; the charter may only raise them. |
| 3 | Production runs as evidence? | Read-only in the brief as context; never in any measurement, which uses program datasets only. |
| 4 | Concurrent programs on one harness? | One active program per harness in v1. |
| 5 | Promoted dataset → the harness's regression eval? | Opt-in export command in v1; default later (§18). |
| 6 | Charter bundles of promotion: all-or-nothing? | Per-change approval inside one bundle, like `proposals apply` today. |
