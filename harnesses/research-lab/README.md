# research-lab

> **Proves:** a director model improves a harness by running experiments it
> cannot grade itself. A deterministic engine measures every idea on a working
> split and keeps only a confirmed one. It reads a sealed split once, and it
> stops at the ceiling where no lever in the charter can reach what still fails.

Offline, no API key. A shipping desk quotes parcel prices from a rate table.
Nine of twenty-four requests give the weight in grams, and the desk passes the
figure to `rate_quote` as kilograms. Five are for contract customers, whose
discount only the billing system knows. The first defect is a prompt's
business. The second is not: the desk has no way to see contracts.

The models are a scripted provider in `extensions/research_lab.py`, and
neither script carries the answer:

- **The clerk (`desk-clerk`)** converts grams only when a rule in its prompt
  says to.
- **The director (`director`)** works through the same tools a real model
  would. It aims at the strongest addressable risk signal in `brief()`, reads
  the failing runs behind it, and names what their requests share. It also
  tries a cheaper generic idea, which gives the engine something to not keep.

Swap `models.director` in `research.yaml` for a real `provider/model` to
watch a model do the same job.

## Capabilities

- **Research programs** (`hiveloom research`): a charter, a
  hash-chained ledger, candidates as full harness copies under their own Hive
  key, and a live harness that is never touched.
- **Typed director tools**: `brief`, `runs`, `excerpt`, `register_hypothesis`,
  `design_experiment`, `interpret`. Each refuses what the charter does not
  allow and says why.
- **Staged, paired experiments**: harm and futility may stop an experiment
  early, and a benefit is read only at the planned size.
- **Budget pools**: exploration, experiments, and a reserved confirmation
  pool.
- **Sealed confirmation**: the holdout is read once, base against final.
  Evidence is `confirmed`, `supported`, or `provisional`.
- **Stop conditions**: goal, rounds, budget, no progress, the director, the
  user, and the **ceiling**.
- **Promotion**: the kept changes are queued as a `trigger=research`
  proposal on the live harness, applied only by `proposals apply`.
  ([autoresearch.md](../../docs/design/autoresearch.md))

## Run it

```bash
hiveloom validate .
hiveloom research init . --name quotes --charter research.yaml --approve --json
hiveloom research run . --name quotes          # or: step, one unit at a time
hiveloom research report . --name quotes
hiveloom proposals list . --json
hiveloom proposals apply . <proposal_id> --yes
hiveloom run . --input-text "Quote shipping for a 2500 g parcel to zone 2 for customer C-100." --json
```

## What to look for

| step | evidence |
|---|---|
| `init` | 18 working cases, 6 sealed; the live harness's id is untouched, `c0` runs under `<id>-r-quotes` |
| baseline | 18 cells; every gram request fails with `tool_error:rate_quote`, every contract request with a verifier failure |
| hypothesize | the director reads `runs(feature=tool_error:rate_quote)`, sees that every request behind the signal is in grams, and registers that alongside "more turns" |
| e1 (grams rule) | `tool_error:rate_quote` 6/18 → 0/18, paired → **confirmed**, kept |
| e2 (more turns) | 6/18 → 6/18 → **inconclusive**, not kept |
| round 2 | the director finds no signal pointing at a lever and designs nothing |
| survey | **ceiling**: 100% of what still fails is content, and nothing in the charter reaches it. The recommendation names the way out: a lever that lets the desk fetch contracts |
| confirm | the sealed split, once: success 2/6 → 5/6, **supported** (six pairs cannot reach significance, and the report says so) |
| report | calibration: the director predicted the change within a point |

## Concepts mode: no eval, only what you care about

`research-concepts.yaml` starts the same desk from `concepts.md` (two
sentences about what matters) and `seeds.jsonl` (three example requests)
instead of an eval. It uses the same scripted provider, which now also
provides the director's framing, the examiner and two judges.

```bash
hiveloom research init . --name concepts --charter research-concepts.yaml --approve --json
hiveloom research run . --name concepts          # frames, then waits for you
hiveloom research contract . --name concepts     # two criteria, six sample cases
hiveloom research approve . --name concepts
hiveloom research run . --name concepts --until unit   # the examiner's sealed cases
hiveloom research run . --name concepts --until unit   # baseline; it asks for labels
hiveloom research questions . --name concepts
hiveloom research answer <question_id> fail --dir . --name concepts   # one per question
hiveloom research run . --name concepts
```

| step | evidence |
|---|---|
| frame | contract: `quoted` (deterministic, `json_present price`) and `one-json` (judge rubric); 17 working cases |
| examine | 8 sealed cases, from the concepts and the contract only |
| baseline | six label questions on `one-json`, where the judges' verdicts need your backing |
| after labels | `one-json` becomes **measured** (κ 1.0 on 6 labels); runs pass only on measured criteria |
| rounds | the grams fix is confirmed; the contract's goal thresholds stop the program |
| report | how each criterion was measured, and the working-vs-sealed gap |

The Research tab in the workbench does all of this through an approval card
and question cards.

## Try this

- `hiveloom research step . --name quotes` repeatedly: every unit persists, so
  a program resumes from its files after an interruption.
- `hiveloom research stop . --name quotes` mid-program: the next unit confirms
  what was kept, then reports.
- Remove `execution` from `research.yaml` and init again: an unclassified code
  tool stops the program before anything runs.

## Reset

Programs live in `.hiveloom/research/`, and their runs are indexed in your Hive
under the program's own key. To start over, remove `.hiveloom/`.
