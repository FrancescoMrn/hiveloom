# article-digest: does the harness help models that are already strong?

The article-extractor benchmark showed scaffolding rescues a model that cannot
hold the output contract (Haiku 3%→65%) and is not measurably useful for one
that can (raw Sonnet: 100%). This companion experiment asks the follow-up:
**on an output-heavy task with more surface for hallucination, does the
harness still earn its place on frontier models (Claude Opus 5, Claude
Sonnet 5)?**

## Task

URL → structured digest (strict JSON):

- `summary` — 120-200 words of **original prose** (must NOT be verbatim page
  text — checked),
- `key_quotes` — exactly 5 **verbatim** passages from the page (checked
  character-for-character against a live re-fetch; ≤1 miss tolerated),
- `outline` — H1-H3 headings verbatim (≤1 miss tolerated),
- `source_url` — exact input URL.

Output is ~5-10× more generated text than article-extractor, so there is real
room for fabrication and drift — the failure mode strong models can still hit.

## Arms

4 arms × 10 URLs × 2 epochs = 80 runs. Both raw and harness arms run through
hiveloom with the **same prompt, tool (`fetch_clean`), guardrails, and loop
policy**; the raw arms have the validators removed and
`loop.require_verification false`. The measured delta is therefore
**validators + retry-with-feedback only** (narrower than article-extractor's
raw arms, which ran outside hiveloom entirely — but cost accounting is
uniform).

| Arm | Model | Validators + retry |
|---|---|---|
| opus-harness | claude-opus-5 | yes |
| opus-raw | claude-opus-5 | no |
| sonnet-harness | claude-sonnet-5 | yes |
| sonnet-raw | claude-sonnet-5 | no |

URLs: article-shaped pages from the shared article-extractor dataset
(`s01 s03 s07 s17 s18 s22 s23 s25 s28 s29`) — news, corporate engineering
blogs, personal blogs. Excluded: reference-doc pages (not digest-shaped),
`s20` (known drift), `s32` (deliberate 404).

## Scoring

`scripts/score.py` applies the **same deterministic checks to every arm**
(the harness validator's own pure `check` function), with one markdown fence
layer stripped for all arms. Success requires all checks; hallucination rate
= runs with ≥2 quotes not found on the live page. Costs come from hiveloom's
accounting (prompt caching included; Opus 5 $5/$25, Sonnet 5 intro $2/$10 per
Mtok — see `~/.hiveloom/models.yaml`; the intro rate expires 2026-08-31).

## Setup and run

```bash
cd evals/article-digest
export ANTHROPIC_API_KEY=sk-...          # or put it in each arm's .env
./scripts/setup_harnesses.sh             # builds harnesses/ from the CLI
python3 scripts/run_eval.py --epochs 2   # ~80 live runs, resumable
python3 scripts/score.py                 # writes RESULTS.md
```

Rough cost estimate: Opus arms ~$0.05-0.10/run, Sonnet arms ~$0.02-0.04/run
→ **~$4-7 for the full sweep** (thinking is on by default on both models and
is billed as output tokens).

## Caveats

- Both models run with **adaptive thinking on** (their API default); hiveloom
  does not set the `thinking` parameter. `max_tokens` is 16000 to leave
  headroom, since it caps thinking + response together.
- This needed two small fixes to `src/hiveloom/models/claude.py` (present on
  this branch): omit `temperature` for models that reject sampling params
  (Opus 4.7+, Sonnet 5, Fable/Mythos), and preserve thinking blocks in
  `content_blocks` so multi-turn tool loops replay validly.
- `claude-opus-5` and the Sonnet 5 intro price are registered in
  `~/.hiveloom/models.yaml` (the builtin catalog predates Opus 5).
- The raw arms keep the `strip_json_fence` hook (it is cosmetic and the
  scorer strips a fence layer for all arms anyway).
- 10 URLs × 2 epochs is a smoke-scale experiment: expect wide confidence
  intervals; treat single-digit deltas as noise. Live URLs can drift between
  the run's fetch and scoring; the ≤1-miss tolerance absorbs most of it.
