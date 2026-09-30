# research-bakeoff — results

Pre-registered go rule (from [the design](../../docs/design/autoresearch.md)): the research
arm fixes **≥ 1.5×** the planted defects of `evolve --experiment`, at a false-promotion
rate **no higher**.

Model for both arms: `openrouter/openai/gpt-5-mini`. The executor is the scripted desk,
which costs nothing. 4 planted harnesses × 2 arms × 3 repeats. Every defect is scored on
40 fresh test cases that neither arm saw.

## Run 1 — 2026-09-29, seed offset 0

| arm | defects fixed | kept changes | false promotions | clean regressions | decoy "fixed" | spend | $ per fixed defect | wall clock |
|---|---|---|---|---|---|---|---|---|
| research | **19/24** | 17 | 0 | 0 | 0 | $0.42 | $0.022 | 29.8 min |
| evolve --experiment | 17/24 | 14 | 0 | 0 | 0 | $0.37 | $0.022 | 20.7 min |

**Ratio 1.12×, so M0 is a NO-GO** under the pre-registered rule. Both arms had a 0%
false-promotion rate.

| harness | research (per repeat) | evolve (per repeat) |
|---|---|---|
| desk-unit-case | 1, 2, 2 | 2, 2, 2 |
| desk-retry-currency | 2, 2, 2 | 1, 1, 1 |
| desk-unit-retry | 1, 2, 1 | 0, 0, 2 |
| desk-case-currency | 1, 1, 2 | 2, 2, 2 |

Per defect (fixed / chances):

| defect | research | evolve |
|---|---|---|
| unit | 4/6 | 4/6 |
| case | 4/6 | 6/6 |
| retry | **5/6** | 2/6 |
| currency | 6/6 | 5/6 |

Where each arm fell short:

- **evolve** kept missing the retry defect. Its single proposal per round aimed at the
  tool error; a retry rule leaves that error in the trace, so the change was judged
  refuted. It fixed retry in only 2 of 6 chances. The research arm keeps a significant
  success gain whose predicted target did not move (`improved`), and fixed retry in
  5 of 6. Five more of evolve's rounds were never applied: the memory entries or
  prompts it wrote made the spec invalid. The research director's changes go through
  the same gate, but a refusal comes back as a tool result it can correct within the
  round.
- **research** lost fixes it had already confirmed. A round designs two experiments
  against the same incumbent. When both were confirmed, only the better one was kept,
  and the brief showed the other as "confirmed", so the director never re-tested it.
  This happened in 3 of the 5 rows where research fixed one defect of two.
- Neither arm touched the contract-customer decoy. The research arm stopped at the
  **ceiling** in 7 of 12 programs, with the recommendation to widen the levers.

### Changes made after run 1 (disclosed)

- Before run 1, one smoke run on `desk-unit-case` showed the live director theorizing
  without reading a single failing run, and failing five times to guess the shape of an
  untyped change. The brief then gained failing examples behind each signal (the
  counterpart of the trace excerpts evolve already gets), and `design_experiment` got a
  typed `{path, value}` schema. Run 1 was made after this fix.
- **After run 1:** the engine now **stacks** a round's other winners. Each is rebased
  onto the kept candidate and measured again. The ceiling now also needs a round that
  tried and kept nothing. Run 2 uses **fresh case sets** (`--seed-offset 100`), so the
  fix is not tuned to run 1's cases.

## Run 2 — seed offset 100, after stacking

| arm | defects fixed | kept changes | false promotions | clean regressions | decoy "fixed" | spend | $ per fixed defect | wall clock |
|---|---|---|---|---|---|---|---|---|
| research | **21/24** | 19 | 0 | 0 | 0 | $0.51 | $0.025 | 35.5 min |
| evolve --experiment | 20/24 | 12 | 0 | 0 | 0 | $0.37 | $0.018 | 20.7 min |

**Ratio 1.05×, so M0 is a NO-GO again.**

| defect | research | evolve |
|---|---|---|
| unit | 6/6 | 6/6 |
| case | **3/6** | 6/6 |
| retry | **6/6** | 3/6 |
| currency | 6/6 | 5/6 |

- Research again won on **retry** and lost on **case**. All three case misses were
  in rounds that confirmed two fixes at once:
  - In two of them, stacking skipped the second fix as a conflict. Both winners had
    re-wrapped the same prompt paragraph before adding their own section, so a
    verbatim rebase found no anchor. This was fixed after run 2, with a three-way
    merge by lines and then by whitespace-insensitive paragraphs, and lessons merged
    by id. Replayed on the saved prompts of those rows, both now merge, with both
    fixes present.
  - In the third, the second fix was never confirmed.
- Evolve again left a retry fix unconfirmed in half its chances, and 7 of its rounds
  were never applied because its spec change was invalid.

## What the two runs say

- **The pre-registered bar cannot be reached on this benchmark.** Evolve fixed 17 and
  then 20 of 24 defects. Even a perfect research arm (24/24) would reach only 1.41×
  and 1.2×, short of 1.5×. The benchmark is too easy at the top to answer the question
  as posed, and a fair go/no-go needs harder planted harnesses: more defects per
  harness, subtler ones, and ones whose evidence must be read rather than counted.
- **The research arm is not worse, and it is different.** It is ahead in both runs
  (19 vs 17, 21 vs 20), with no false promotions and the decoy left alone every time.
  It found the retry defect in 11 of 12 chances, against evolve's 5 of 12, because it
  keeps a real success gain whose predicted mechanism did not move. It lost fixes to
  its own round structure, which is now fixed and waiting for a run on fresh cases.
- **Its cost is at parity per fixed defect** ($0.022–0.025 against $0.018–0.022),
  but it takes 1.4–1.7× the wall clock.

### The question, revised (2026-09-30)

The 1.5× rule asked whether a director should *replace* evolve. The product owner
has since set the question the feature is actually for. Evolve is the deliberate,
manual step, and autoresearch runs that step repeatedly and unattended. It must
be **as good as a person running evolve, without the person, and safe while
nobody watches**. On that question both runs pass:
- never fewer defects fixed (+2, then +1);
- 0 false promotions in 24 programs;
- the decoy never "fixed";
- cost at parity per fixed defect.

This is a post-hoc change of criterion, recorded as one. The pre-registered 1.5×
verdict above stands as what it was. The feature ships as
`hiveloom evolve --research`, and its next test is an autonomy benchmark: long
unattended runs, below.

## M2 live check: do real judges earn trust from a user's labels?

`scripts/concepts_live.py` ran research-lab in concepts mode. The director framed the
contract with `gpt-5-mini` and the examiner was `gemma-4-31b-it`. The two judges were
`gpt-5-mini` and `gemma-4-31b-it`. A test user approved a contract with one
deterministic criterion and one judged criterion, and answered the label questions
truthfully.

| | |
|---|---|
| judged criterion `one-json` | **trusted: κ 1.0, agreement 1.0 on 11 labels**, within the 12-question budget |
| sealed cases written by the examiner | 8 |
| judge calls / spend | 64 / $0.019 |
| whole check | ≈ $0.04, 4.6 min |

The live director's own draft contract is the other finding. It invented a rate table
that exists nowhere in the harness, and packed three fields into one `json_present`
check. The approval gate, where a user reads the criteria and sample cases, is where
this gets caught. The engine now refuses a field list, and the frame task forbids facts
the concepts do not give.

## Autonomy benchmark — 2026-09-30, seed offset 300

Each planted harness got one unattended program, run as
`hiveloom evolve --research --yes`: 10 rounds allowed, a $0.60 budget, and a live
`gpt-5-mini` director. That makes 4 harnesses × 2 repeats, scored on 40 fresh cases.

| property | held |
|---|---|
| no drift (never worse on fresh cases) | **8/8** |
| within budget (every pool) | **8/8** |
| stops itself (not on the round limit) | **8/8** |
| no false keeps | **8/8** |
| stops on time (≤ no_progress + 1 rounds after the last keep) | **8/8** |

- **Defects fixed: 15/16.** Fresh-case success went from 25% to 80% in 7 of the 8
  programs. 80% is this benchmark's maximum, because the remaining 20% are the
  contract-customer decoy that no prompt reaches. The eighth program reached 52.5%
  (retry missed) and stopped on no progress.
- **Stops:** 7 at the ceiling, which correctly named the decoy as out of reach, and
  1 on no progress. Programs used 18 of the 80 rounds allowed: they stop when there
  is nothing left to do, not when the allowance runs out.
- **Spend:** $0.26 in total, $0.022–0.054 per program. No pool spent more than 36%
  of its share.
- **Applied unattended:** 8/8, since every promotion's evidence was confirmed (6) or
  supported (2).

Compared with the head-to-head runs above (19/24 and 21/24), these programs ran on
the engine after the stacking and three-way-merge fixes, and on fresh cases.

## Autonomy with a real executor — 2026-09-30, seed offset 500

This is the same autonomy run, except the desk clerk is a real, cheap model
(`mistralai/ministral-3b-2512`, set on each copy with `hiveloom set model`), not the
scripted one. So executor costs are real, answers are noisy, and the model brings
defects nobody planted. The main one: it wraps its JSON in markdown code fences,
which billing cannot parse, so **every** answer failed at the start, clean cases
included. The setup was 4 harnesses × 2 repeats, a live `gpt-5-mini` director,
10 rounds allowed, and a $0.60 budget each.

| property | held |
|---|---|
| no drift (never worse on fresh cases) | **8/8** |
| within budget (every pool, executor spend included) | **8/8** |
| stops itself | **8/8** (ceiling 4, no progress 4) |
| stops on time | **8/8** |
| no false keeps (every kept step beats its parent on fresh cases) | **6/8**, see below |

- **Fresh-case success went from 0% to 58% on average.** The range was 40–80%, and
  the maximum possible is 80% (the contract decoy). Every program found and fixed the
  code-fence defect first.
- **It used 28 of the 80 rounds allowed**, and $0.51 in total, executor included.
  All 8 promotions were applied unattended, with 6 confirmed and 2 supported.
- **The two P4 exceptions are different in kind:**
  - **A real false keep** (desk-case-currency #2, e1). The change was "confirmed"
    because verifier failures fell from 30/30 to 0/30, but success stayed at 0/30:
    runs stopped answering and hit the turn limit, so there was nothing left to
    verify. A later change (more turns) turned it into real success. **Fixed:** the
    engine now labels such a change `shifted` and does not keep it. Replayed on this
    run's saved programs, it flags exactly this step.
  - **A stepping stone** (desk-case-currency #1, e3). Zone normalization removed
    every tool error (8/30 to 0/30), but those runs still failed on another defect
    (currency), so success did not move until that was fixed too. The chain ended
    at 55% from 0%. This is a legitimate keep, and the per-step P4 is too strict
    for it. It stays counted as a miss, because an ablation would be needed to
    tell it apart cleanly.
