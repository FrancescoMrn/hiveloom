# Results: article-extractor benchmark

Nine arms, five models, hiveloom **0.3.1** (`72b9a45`). 32 golden samples x 3
epochs = 96 runs per arm, 864 runs total, dataset `0970a74`.

Same task, same system prompt, same `fetch_clean` tool in every arm. Harness
arms shell out to `hiveloom run --json`, so they measure the shipped pipeline;
raw arms reuse the harness prompt verbatim, so the delta is scaffolding only:
validators, retry-with-feedback, guardrails, loop policy.

|---|---|---|---|---|---|---|---|---|---|---|---|
| haiku_harness | 96 | 65% (55–73) | 11% | 67% | 99% | 89% | 0.89 | $0.0120 | $0.0186 | 10.3 / 14.1 | 62% |
| haiku_raw | 96 | 3% (1–9) | 96% | 4% | 5% | 5% | 0.05 | $0.0069 | $0.2212 | 5.8 / 7.2 | 0% |
| sonnet_raw | 96 | 100% (96–100) | 0% | 100% | 97% | 86% | 1.00 | $0.0073 | $0.0073 | 6.4 / 8.1 | 100% |
| qwen_harness | 96 | 69% (59–77) | 16% | 94% | 90% | 84% | 0.64 | $0.0000 | $0.0000 | 6.3 / 12.2 | 69% |
| qwen_raw | 96 | 58% (48–68) | 19% | 96% | 95% | 86% | 0.71 | $0.0000 | $0.0000 | 3.9 / 6.0 | 53% |
| gemma_harness | 96 | 90% (82–94) | 0% | 90% | 97% | 87% | 1.00 | $0.0000 | $0.0000 | 9.3 / 14.9 | 88% |
| gemma_raw | 96 | 92% (84–96) | 1% | 91% | 92% | 86% | 0.97 | $0.0000 | $0.0000 | 26.2 / 44.6 | 84% |
| qwen35_harness | 96 | 84% (76–90) | 0% | 84% | 97% | 90% | 0.98 | $0.0000 | $0.0000 | 16.4 / 53.0 | 84% |
| qwen35_raw | 96 | 75% (65–83) | 16% | 81% | 97% | 87% | 0.91 | $0.0000 | $0.0000 | 13.5 / 35.0 | 75% |


## Does the harness earn its place?

Each row compares the two arms on identical inputs. **The unit of analysis is the
URL, not the run**: the 96 runs per arm are 32 pages measured 3 times, and site
difficulty correlates strongly within a page, so treating the 96 as independent
overstates significance. The paired test below is a Wilcoxon signed-rank over
the 32 per-URL success rates. An earlier version of this file reported
run-level McNemar and called the qwen3:4b result significant; clustered
properly it is not.

| Model | harness | raw | delta | URLs better/worse/tied | paired p |
|---|---|---|---|---|---|
| claude-haiku-4-5 | 64.6% | 3.1% | **+61.5** | 21 / 1 / 10 | **<0.0001** |
| qwen3:4b-instruct | 68.8% | 58.3% | +10.4 | 6 / 2 / 24 | 0.18 (n.s.) |
| qwen3.6-35B-A3B | 84.4% | 75.0% | +9.4 | 6 / 3 / 23 | 0.51 (n.s.) |
| gemma4:12b | 89.6% | 91.7% | -2.1 | 4 / 3 / 25 | 0.86 (n.s.) |

**Exactly one of four harness benefits is statistically defensible at this sample
size.** Haiku's is large and unambiguous: raw haiku wraps its JSON in prose and
nothing tells it to stop, and retry-with-feedback recovers it on 21 of 32 pages.
The other three deltas point the way you would expect but are indistinguishable
from noise over 32 pages, and should not be quoted as evidence that the harness
helps those models. Note also the ceiling: a model with ~1% raw error has almost
no room for a positive delta, so gemma's -2.1 is not evidence of harm either.

What this benchmark can support is therefore narrower than the arm table
suggests: scaffolding rescues a model that cannot hold the output contract, and
is not measurably useful for models that already can.

**Hallucination is the more robust signal**, because the effect sizes are large
relative to the sample:

| Model | harness | raw |
|---|---|---|
| claude-haiku-4-5 | 11% | 96% |
| qwen3:4b-instruct | 16% | 19% |
| qwen3.6-35B-A3B | **0%** | 16% |
| gemma4:12b | **0%** | 1% |

**The incumbent still wins outright.** sonnet-5 raw scores 100% with 0%
hallucination at $0.0073 per success; haiku+harness costs $0.0186. Scaffolding a
cheap model does not beat using a better one on this task. What it buys is a
local, $0, private option landing within 10 points of a frontier model.

### What this benchmark does not measure

Stated plainly, because the arm table invites over-reading:

- **"Task success" is narrower than it sounds.** It requires schema validity,
  the hallucination check, and an exact title match — not correct author, date,
  description, or headings, which are reported as separate columns
  (`inspect_evals/scorer.py`). An output with the right title and wrong metadata
  counts as a success. This is closer to a title-copy benchmark with a schema
  gate than to full structured extraction.
- **The harness gets corrective model calls that raw does not.** Retry-with-
  feedback means up to two extra calls carrying validator output. That is the
  mechanism under test, but it means the comparison shows "retrying against the
  scoring rule rescues a weak model", not that the harness encodes durable task
  knowledge.
- **One task, public web pages.** It cannot speak to tasks whose data cannot
  leave the building, which is the case the product is actually aimed at.

## What changed since the pre-0.3.0 sweep

Same dataset, same parameters, same hardware. Only hiveloom changed.

| Arm | pre-merge | 0.3.1 | |
|---|---|---|---|
| haiku_harness | 61% | 64.6% | +3.6 |
| haiku_raw | 2% | 3.1% | +1.1 |
| sonnet_raw | 99% | 100.0% | +1.0 |
| qwen_harness | 69% | 68.8% | -0.2 |
| qwen_raw | 60% | 58.3% | -1.7 |
| **gemma_harness** | **78%** | **89.6%** | **+11.6** |
| gemma_raw | 94% | 91.7% | -2.3 |
| qwen35_harness | 83% | 84.4% | +1.4 |
| qwen35_raw | 78% | 75.0% | -3.0 |

Eight of nine arms moved within +-3.6 points, which is noise at n=96. One moved
11.6, and it is the arm that closes the only case where the harness used to
*subtract*: gemma4:12b harnessed was 16 points below raw, and is now level with
it.

The cause is a provider bug, not a scaffolding change. gemma via Ollama returns
its text in the `reasoning` field with `content: ""`, which normalised to an
empty assistant turn. On a single raw call that costs almost nothing; inside a
multi-turn loop with retry-with-feedback it degraded every retry cycle. One
sample (s23) died outright on `invalid message content type: <nil>`; it now
passes 3/3, and the harness arm's error count is 0.

The control is qwen3:4b, which stayed flat at 68.8%. It is an instruct model and
never emits reasoning-only replies, so it never hit the bug. That is what makes
this attributable to the reasoning-field fix rather than to general
improvements: the arms that moved are exactly the ones that could have been
affected.

Residual: both gemma arms still strip site-name suffixes ("... [LWN.net]",
"... \ Anthropic") against a verbatim-copy contract, but symmetrically now, 6
harness-only title misses against 5 raw-only, versus 15 against ~0 before. That
is model behaviour, not a scaffolding bias.

## Caveats

- Pricing (USD/Mtok, as of 2026-07-29): claude-haiku-4-5 (1.0, 5.0), claude-sonnet-5 (2.0, 10.0), qwen3:4b-instruct (0.0, 0.0), gemma4:12b-mlx (0.0, 0.0), Qwen3.6-35B-A3B-8bit (0.0, 0.0). Sonnet 5 is at the introductory rate through 2026-08-31.
- Local arms (qwen, gemma, qwen35) cost $0 by definition (local inference; hiveloom also counts usage-less openai-compat responses as free) — latency is their resource proxy. qwen3:4b and gemma4:12b are served by native Ollama (Metal); qwen3.6-35B-A3B by mlx_lm.server.
- The scorer strips one markdown fence layer for all arms (lenient to raw arms).
- Harness-arm cost comes from hiveloom's own accounting; raw-arm cost from inspect_ai token usage × the pricing table (cache writes at 1.25x input, reads at 0.1x).
- A `*` on a cost cell means some rows in that arm had no price (unpriced model or timeout) and are excluded from the cost math.
- Live-URL dataset: see the drift check run alongside this sweep.

Logs: `logs/haiku_harness/2026-07-31T08-54-15-00-00_article-extractor-harness_UiVRC7Ta9oQfRbcgfhrsih.eval`, `logs/haiku_raw/2026-07-31T08-58-06-00-00_article-extractor-raw_55K6YyTKdjqCNe4rWcnoiJ.eval`, `logs/sonnet_raw/2026-07-31T09-00-05-00-00_article-extractor-raw_APPPvUZuJ7LpYjb2LT2R3u.eval`, `logs/qwen_harness/2026-07-31T09-02-36-00-00_article-extractor-harness_Tobz6Nvfn2EuMVzCPPjXZQ.eval`, `logs/qwen_raw/2026-07-31T09-17-07-00-00_article-extractor-raw_9qqgamvGuvh2gsfgtsUm4Y.eval`, `logs/gemma_harness/2026-07-31T07-30-26-00-00_article-extractor-harness_58VWKWHTwwBQf2ByQ77TwU.eval`, `logs/gemma_raw/2026-07-31T07-49-46-00-00_article-extractor-raw_N7jfJHdNe5GSYvaCcrDMfz.eval`, `logs/qwen35_harness/2026-07-31T09-24-36-00-00_article-extractor-harness_Mp9mQSQgTnAsqfuSuzkqrA.eval`, `logs/qwen35_raw/2026-07-31T10-07-39-00-00_article-extractor-raw_9pKrwEh8pNXQjupQuhFABo.eval`
