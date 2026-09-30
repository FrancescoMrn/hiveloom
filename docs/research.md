# Research programs: evolving autonomously

`hiveloom evolve` is the deliberate step. You run it, read the proposal, and
decide. A research program is the same step run **autonomously**: an external
director runs it round after round, unattended, and you come back to one
measured proposal. Start one with:

```bash
hiveloom evolve ./h --research --json                 # uses ./h/research.yaml, or builds a charter
hiveloom evolve ./h --research --model openrouter/openai/gpt-5-mini --budget 2 --rounds 6 \
    --tool http_get=replay --json                      # a charter from the harness itself
hiveloom evolve ./h --research --program NAME --json   # resume an interrupted run
hiveloom evolve ./h --research --yes --json            # also apply it, if confirmed or supported
```

`hiveloom research …` (below) is the full interface. The workbench's Improve tab
offers the same as **Evolve autonomously**.

A research program improves a harness through experiments instead of guesses.
Two agents take part:

- the **executor** is the harness itself, unchanged in how it runs;
- the **director** is a model, working inside its own confined harness, that
  reads the executor's evidence, forms hypotheses, and designs changes.

The director never grades its own ideas. A deterministic **engine** does
everything else:
- runs the evals and debits the budget;
- measures each experiment pair by pair and decides its verdict;
- keeps or discards candidates and checks the stop conditions;
- reads the sealed cases once;
- queues the result for review.

The live harness is never touched: the output is a proposal you apply with
`proposals apply`.

```
charter ──► init ──► baseline ──► survey ──► hypothesize ──► experiment … ──► interpret ─┐
 (you)      (free)    (engine)    (engine)    (director)       (engine)       (director) │
                                     ▲                                                   │
                                     └───────────────────────────────────────────────────┘
                     a stop condition ──► confirm (sealed, once) ──► report ──► proposal
```

[`harnesses/research-lab`](../harnesses/research-lab) runs the whole loop
offline, with no API key: it finds a planted defect from the runs behind a
signal, keeps the fix, discards a generic idea, and stops at a ceiling that no
prompt can reach. The design, including the milestones still to come, is in
[design/autoresearch.md](design/autoresearch.md).

`hiveloom evolve --experiment` is the same idea with one proposal per round
and no director. A research program adds:
- a model that investigates before it proposes;
- hypotheses registered before they are tested, and refused if already
  refuted;
- several experiments per round;
- sequential looks that stop a bad experiment early;
- budget pools;
- a sealed confirmation;
- stop conditions that include knowing when a harness has hit its ceiling.

## 1. The charter: `research.yaml`

The charter is yours. The engine validates it and never writes it.

```yaml
goal: Every quote is what billing will invoice.   # prose, for the director and the report
eval: eval.yaml            # an eval in the harness folder that runs it ('harness: .')
holdout: 0.25              # share of cases sealed for the one confirmation (0..0.8)
levers:                    # the only spec paths the director may change
  - system_prompt
  - loop.max_turns
guard:
  success_rate: no_regression
  cost_per_run_increase: 0.2      # optional: +20% mean cost per run at most
budget:
  usd: 1.0                 # the whole program: director, executor runs, confirmation
  wall_clock_minutes: 240
  rounds: 6
  split: {exploration: 0.25, experiments: 0.60, confirmation: 0.15}
stop:
  goal: {success_rate: 0.95}      # or metric:<name>: <mean>
  no_progress_rounds: 3
  ceiling_content_share: 0.7
execution:
  tools: {rate_quote: allow}      # allow | sandbox | replay | deny
  mcp: {}                         # server: mode, or server: {tool: mode}
  max_cost_per_run: 0.05          # an extra max_cost_usd on every executor run
models:
  director: openrouter/openai/gpt-5-mini
experiments_per_round: 2
```

`init` refuses a charter, listing every problem, when:
- a lever is not in the harness's `evolution.mutable`, or touches a frozen
  path (the safety layer is never a lever);
- the eval does not run this harness;
- fewer than two working cases remain after the holdout;
- a tool with effects the engine cannot bound is unclassified (see §4).

## 2. Commands

All take `--json` and use the usual exit codes (`3` for a refused charter).

| command | does |
|---|---|
| `hiveloom research init <dir> --name N --charter research.yaml [--approve]` | validate, freeze the working/sealed split, copy the harness as `c0`; nothing runs or is spent |
| `hiveloom research step <dir> --name N` | advance exactly one unit |
| `hiveloom research run <dir> --name N [--until done\|round\|unit]` | run to the end, one round, or one unit; resumable |
| `hiveloom research status <dir> [--name N]` | unit, round, budget pools, experiments, stop reason, promotion, ledger check; without `--name`, every program |
| `hiveloom research report <dir> --name N` | the finished report |
| `hiveloom research stop <dir> --name N [--reason R]` | the next unit confirms what was kept and reports |
| `hiveloom research contract <dir> --name N` | concepts mode: the drafted or approved contract, sample cases, and how each criterion is measured |
| `hiveloom research approve <dir> --name N [--contract edited.yaml]` | concepts mode: the approval gate |
| `hiveloom research questions <dir> --name N [--all]` | concepts mode: what the program is asking you |
| `hiveloom research answer <question_id> <answer> --dir <dir> --name N` | `pass`/`fail` for a label; words or an option otherwise |

Every unit persists before it returns, and an eval records its id before its
first cell runs. An interrupted program therefore continues from its files with
the next `run`: it resumes the interrupted eval rather than paying for it again,
and records a single read of the sealed split.

One process advances a program at a time. A second `run` or `step` gets a
`ProgramBusy` error (exit code 4). `stop` works from any process at any moment,
and the running unit finishes before the program confirms and reports.

## 3. The program folder

```
<harness>/.hiveloom/research/<name>/
    research.yaml        the charter
    ledger.jsonl         hash-chained record of every event (status verifies it)
    state.json           engine-written only
    candidates/c0, c1…   full copies of the harness, one per version tried
    director/            the director's own run journals
    replay.jsonl         recorded tool results, when a tool is replayed
    report.md            written at the end
```

Every candidate carries a program-scoped harness id (`<id>-r-<name>`), so its
runs sit under their own key in the Hive. They never mix with the live
harness's production runs, and production evidence never leaks into the
program's measurements.

## 4. Research-safe execution

A program runs the executor many times on inputs nobody typed for real. Every
research run therefore goes through a policy that can only tighten it:

| mode | behavior |
|---|---|
| `allow` | the tool runs as the harness declares it |
| `sandbox` | effects stay in the candidate's own copy: `file_write`, `notes`, `propose_memory`; `shell` only under `confinement.network: false` |
| `replay` | serves results recorded from the base harness's real runs, keyed by input; a miss is a tool error, never a live call |
| `deny` | the tool is removed from the run |

Read-only builtins default to `allow`, and file writes to `sandbox`. Network,
shell, code and MCP tools have no trustworthy default. The charter must
classify each one, or `init` stops with the list. The harness's own egress
redaction and confinement stay in force.

When `tools` is a lever, the policy still holds for every candidate:
- a change that adds a tool the charter does not classify is refused;
- so is a change to what an effectful tool does, such as more hosts, other
  code, or another command;
- the MCP servers cannot change.

A candidate may still rewrite a tool's description, drop a tool, or add a
read-only builtin. Each candidate runs under the policy resolved from its own
tools.

Delegation is off in every research run. A peer runs its own tools outside
this policy, so `delegation` cannot be a lever, and a harness that delegates
has it switched off in `c0`. The live harness keeps its delegation.

## 5. How a round works

1. **survey**. The engine checks the stop conditions (§7), then starts a
   round.
2. **hypothesize**. The director runs with the tools below. Each tool either
   acts or returns `{"refused": reason}`:

   | tool | does |
   |---|---|
   | `brief()` | goal, levers, guard, budget pools, **power plan**, the incumbent spec, its signal map (addressability judged against the charter's levers), **evidence** (failing runs behind each top signal: task, output, first error), hypotheses, verdicts, calibration, findings, confirmed changes not yet in the incumbent, earlier programs |
   | `runs(status, feature)`, `excerpt(run_id)`, `diff(candidate)` | working-split evidence only; nothing sealed |
   | `register_hypothesis(claim, levers, target, expect, falsifier, prior, by)` | refused if a lever is outside the charter, the target is not in the signal map, or the same idea was already refuted, regressed or futile |
   | `design_experiment(hypothesis_id, changes)` | applies the changes to a copy of the incumbent through the evolution gate; refused if outside the levers, identical to a harness already tested, or unaffordable |
   | `move_budget(source, target, usd)` | between the data, exploration and experiments pools (never confirmation), at most 25% of the total per pool |
   | `note(text)` | a finding for the ledger |

3. **experiment**. Each candidate runs the working split in three looks, and
   is paired case by case with the incumbent's runs.
   - **Harm** (a significant drop in success) stops it at any look.
   - **Futility** stops it once neither its target nor the success rate could
     still reach significance.
   - A **benefit** is only read at the full size, so looking early cannot
     manufacture one.
4. **interpret**. The engine keeps the best eligible candidate as the new
   incumbent. The director then reads the verdicts and calls
   `interpret(findings, next_focus, decision)`: continue, pivot, or stop.

### Verdicts

| verdict | meaning | kept |
|---|---|---|
| `confirmed` | the predicted target moved as predicted, and the guard held | yes |
| `improved` | the success rate rose significantly, but the predicted target did not move; the mechanism was wrong or unmeasurable (a retried call still logs its first error) | yes, ranked below `confirmed` |
| `refuted` / `inconclusive` | no measured effect worth keeping | no |
| `futile` | stopped early: even a perfect remainder could not have shown it | no; the idea is not re-tested without new evidence |
| `shifted` | the predicted mechanism fell only because runs stopped answering (the turn limit, an error, a halt): the failures moved, they did not go away | no |
| `regressed` | the guard failed, or harm stopped it | no; never re-tested without new evidence |

When the director gave a predicted size (`by`), calibration records the
predicted and measured effects.

## 6. Budget

The budget splits into three pools:

| pool | pays for |
|---|---|
| **exploration** | director runs; each one is capped at what is left |
| **experiments** | baseline and experiment cells |
| **confirmation** | reserved for the sealed read, and never movable |

Every cost is a ledger debit. The power plan in the brief is computed from
the baseline's measured cost per run and the working split. It states what
one experiment costs, the smallest success-rate change it can detect, and how
many experiments the pool still buys. That is why the director is told to aim
at counted failure mechanisms when a success-rate change is too small to
detect.

## 7. Stop conditions

Checked before every round. The first one to hold wins:

| condition | rule |
|---|---|
| `goal` | every `stop.goal` threshold holds on the working split |
| `rounds` / `time` | `budget.rounds` done, or `wall_clock_minutes` elapsed |
| `budget` | the exploration pool is spent, or the experiments pool cannot buy another experiment |
| `no_progress` | `no_progress_rounds` rounds without a kept change |
| **`ceiling`** | at least `ceiling_content_share` of what still fails is `content` (the run finished and was wrong), and no risk signal or failure mechanism has a lever in this charter |
| `director` | `interpret` returned `stop` |
| `user` | `hiveloom research stop` |

A ceiling stop comes with a recommendation rather than silence: a stronger
executor model (frozen from evolution, so that's your call), levers wide
enough to fetch what the model lacks, or a narrower task.

## 8. Confirmation, report, promotion

- **Confirm.** If a change was kept, the engine reads the sealed cases
  **once**, the base against the final incumbent. The read is a `sealed_read`
  ledger event. Evidence strength is:
  - `confirmed`: significant on the sealed cases;
  - `supported`: pointing the right way, but too few pairs to be significant;
  - `provisional`: not better, or the confirmation pool could not pay;
  - `contradicted`: significantly worse.
- **Report.** `report.md` records:
  - the stop reason and any recommendation;
  - the budget by pool;
  - every experiment and hypothesis;
  - calibration;
  - the confirmation;
  - the director's findings;
  - the promotion.
- **Promotion.** What the program changed (from `c0` to the final incumbent) is
  merged three-way onto the live harness, gated, and queued as a
  `trigger=research` proposal with the evidence attached. The merge keeps any
  edit you made to the live harness while the program ran. If the program and
  your edit rewrote the same text, the proposal is queued as `rejected`, and
  it names the conflicting path.
  Nothing is applied until you run:

  ```bash
  hiveloom proposals show . <id> --json
  hiveloom proposals apply . <id> --yes
  ```

## 9. Director models

`models.director` is any `provider/model` hiveloom can run. The director
harness ships inside the package (`hiveloom/research/director/`). It is
confined, has no network or shell tools, and has a hard cost cap of whatever
the exploration pool has left. Its runs are journalled under the program's
`director/` folder, like any other run.

## 10. In the workbench

The workbench's **Research** tab (and **Evolve autonomously** in Improve) does
everything the CLI does:
- a charter form built from the harness, or the YAML;
- background runs with a live progress bar, resumed if the server restarts;
- per-case before/after runs that open in Trace;
- contract approval and question cards, and a rail badge when a program needs
  you;
- the queued proposal handed to Improve.

The copilot starts, follows and relays programs in conversation. See
[workbench.md](workbench.md). Front ends drive programs through
`hiveloom.research.service`.

## 11. Concepts mode: no eval, only what you care about

A charter can bring `concepts` (prose, or a file in the harness folder) and
optional `seeds` (a JSONL of example requests) instead of an `eval`. The
program then builds its own evaluation, and keeps it honest:

```yaml
goal: Every quote has a price, in the JSON billing parses.
concepts: concepts.md
seeds: seeds.jsonl                   # optional
budget:
  usd: 1.0
  split: {data: 0.2, exploration: 0.25, experiments: 0.4, confirmation: 0.15}
models:
  director: openrouter/openai/gpt-5-mini
  examiner: openrouter/google/gemma-4-31b-it      # must differ from the director
  judges: [openrouter/openai/gpt-5-mini, openrouter/google/gemma-4-31b-it]
human: {questions: 12, batch: 4}     # how much of your attention it may ask for
trust: {kappa: 0.6, agreement: 0.85, min_anchors: 8}
```

1. **frame**. The director drafts an **evaluation contract**, which is a
   short list of criteria. Each criterion has a check:
   - deterministic: `json_field`, `json_present`, `contains`,
     `not_contains`, `exact`, `regex`;
   - or a judge `rubric`, only where code cannot see the thing.

   The director also writes working cases for the criteria, and may `ask` you
   what a concept means.
2. **approve**. This is a hard gate. Read the contract and its sample cases,
   then approve it, or approve an edited version. Nothing is spent on changes
   before this.
3. **examine**. The examiner is a different model, and it never sees the
   working cases. It writes **sealed** cases from the concepts and the
   approved contract. A working case that nearly copies a sealed one is
   dropped, and only the count reaches any record the director reads.
4. **baseline and rounds**, as with a given eval. Every run is scored per
   criterion. Whether a run *passed* is decided by the criteria that are
   **measured** right now:
   - deterministic criteria are always measured;
   - judged criteria are measured only while the judges, who must be
     unanimous, agree with your labels: at least `min_anchors` labels, with κ
     and agreement at or above the floors.

   The engine asks you for those labels itself, disagreements first. Every
   round it audits the incumbent's outputs, so a judge that stops agreeing
   with you loses its trust mid-program. The contract's `goal_thresholds` are
   a stop condition.
5. **confirm and report**. The sealed read counts only measured criteria.
   - Evidence is downgraded from `confirmed` to `supported` if a criterion was
     never measured.
   - The **proxy gap** (working against sealed success of the final version)
     is reported, and flagged when the working cases say much more than the
     sealed ones. This is how a director that games its own cases is caught.
   - Open questions are withdrawn.

## 12. Not yet built

- Growing the dataset mid-program (a new dataset version re-measures the
  incumbent).
- In the workbench, candidates listed in Versions.
- The optional critic and coaching (M5).

Findings are already shared across programs: `brief().earlier_programs` tells a
new director what each finished program on the same harness kept, ruled out,
and stopped on.
