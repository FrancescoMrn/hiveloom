# Results: article-extractor benchmark

Generated 2026-07-29 · dataset `30b99e5` · repo `30b99e5`

| Arm | n | Task success (95% CI) | Halluc. | Title | Author | Date | Headings F1 | Mean cost | Cost/success | p50/p90 lat (s) | pass^k |
|---|---|---|---|---|---|---|---|---|---|---|---|
| haiku_harness | 96 | 61% (51–71) | 11% | 63% | 99% | 89% | 0.89 | $0.0120 | $0.0196 | 10.7 / 16.4 | 59% |
| haiku_raw | 96 | 2% (1–7) | 98% | 2% | 3% | 2% | 0.02 | $0.0071 | $0.3405 | 6.6 / 11.5 | 0% |
| sonnet_raw | 96 | 99% (94–100) | 0% | 100% | 97% | 86% | 1.00 | $0.0076 | $0.0077 | 6.4 / 9.2 | 97% |
| qwen_harness | 96 | 69% (59–77) | 17% | 92% | 89% | 82% | 0.66 | $0.0000 | $0.0000 | 5.9 / 14.0 | 66% |
| qwen_raw | 96 | 60% (50–70) | 18% | 95% | 96% | 87% | 0.72 | $0.0000 | $0.0000 | 3.3 / 5.4 | 56% |
| gemma_harness | 96 | 78% (69–85) | 1% | 80% | 99% | 86% | 0.99 | $0.0000 | $0.0000 | 6.7 / 26.8 | 75% |
| gemma_raw | 96 | 94% (87–97) | 2% | 94% | 95% | 86% | 0.98 | $0.0000 | $0.0000 | 24.1 / 39.7 | 84% |
| qwen35_harness | 96 | 83% (75–89) | 0% | 83% | 97% | 91% | 0.97 | $0.0000 | $0.0000 | 18.2 / 43.8 | 81% |
| qwen35_raw | 96 | 78% (69–85) | 13% | 87% | 100% | 97% | 0.94 | $0.0000 | $0.0000 | 15.4 / 31.1 | 78% |

## Paired comparison

**haiku_harness vs sonnet_raw**: 96 paired runs; harness-only wins b=1, raw-only wins c=37; exact McNemar p=0.0000

**haiku_harness vs haiku_raw** (harness contribution): 96 paired runs; harness-only wins b=57, raw-only wins c=0; exact McNemar p=0.0000

## Caveats

- Pricing (USD/Mtok, as of 2026-07-29): claude-haiku-4-5 (1.0, 5.0), claude-sonnet-5 (2.0, 10.0), qwen3:4b-instruct (0.0, 0.0), gemma4:12b-mlx (0.0, 0.0), Qwen3.6-35B-A3B-8bit (0.0, 0.0). Sonnet 5 is at the introductory rate through 2026-08-31.
- Local arms (qwen, gemma, qwen35) cost $0 by definition (local inference; hiveloom also counts usage-less openai-compat responses as free) — latency is their resource proxy. qwen3:4b and gemma4:12b are served by native Ollama (Metal); qwen3.6-35B-A3B by mlx_lm.server.
- The scorer strips one markdown fence layer for all arms (lenient to raw arms).
- Harness-arm cost comes from hiveloom's own accounting; raw-arm cost from inspect_ai token usage × the pricing table (cache writes at 1.25x input, reads at 0.1x).
- A `*` on a cost cell means some rows in that arm had no price (unpriced model or timeout) and are excluded from the cost math.
- Live-URL dataset: see the drift check run alongside this sweep.

Logs: `logs/haiku_harness/2026-07-29T13-19-47-00-00_article-extractor-harness_kZF8TvtGFUPQJL4gLCwgs5.eval`, `logs/haiku_raw/2026-07-29T13-24-00-00-00_article-extractor-raw_2vhoers8Tnh83aFKrsBCnn.eval`, `logs/sonnet_raw/2026-07-29T13-27-22-00-00_article-extractor-raw_Emdqcb6GkhQM6GU9BkKHeJ.eval`, `logs/qwen_harness/2026-07-29T15-49-45-00-00_article-extractor-harness_S6sgonYGXzQQNdCFUiowQH.eval`, `logs/qwen_raw/2026-07-29T15-39-06-00-00_article-extractor-raw_mDSSF699NVQoQtvNFCYwT3.eval`, `logs/gemma_harness/2026-07-29T16-06-01-00-00_article-extractor-harness_ZCzskKW8Aycx4s8cEw2786.eval`, `logs/gemma_raw/2026-07-29T17-01-00-00-00_article-extractor-raw_88CGrBwMnvXFcJiKCYRHvw.eval`, `logs/qwen35_harness/2026-07-29T17-54-59-00-00_article-extractor-harness_WbMYLMrSs7c963YCvEJEC9.eval`, `logs/qwen35_raw/2026-07-29T18-51-20-00-00_article-extractor-raw_6hBqam3NjhpETBtmTJNSxc.eval`

## Re-test against 0.3.1 vs the pre-merge sweep

Same model, same 32 samples x 3 epochs, same 4096-token budget. Only the
hiveloom under the harness arm changed.

| Metric | pre-merge | 0.3.1 | |
|---|---|---|---|
| gemma + harness | 78% (69-85) | **90% (82-94)** | +12 |
| gemma raw | 94% (87-97) | 92% (84-96) | -2 (CIs overlap) |
| harness title fidelity | 80% | **90%** | +10 |
| harness hallucination | 1% | **0%** | |
| harness headings F1 | 0.99 | **1.00** | |
| harness pass^3 | 75% | **88%** | +13 |
| errored runs in harness arm | 1 (s23, HTTP 400) | **0** | |

Paired over the 96 runs: harness-only wins b=5, raw-only wins c=7, exact
McNemar p=0.77 — no significant difference between the two arms. The 16-point
deficit that motivated issue #6 is gone.

What changed the harness arm, in likely order of size: gemma via Ollama returns
its text in `reasoning` with `content: ""`, which used to normalise to an empty
assistant turn (issue #5). On a multi-turn loop with retry-with-feedback that
cost more than the one crashed sample it was found through. s23, which died on
`invalid message content type: <nil>`, now passes 3/3.

Title fidelity is no longer a harness bias. Both arms still strip site-name
suffixes ("... [LWN.net]", "... \ Anthropic") at similar rates: 6 harness-only
title misses vs 5 raw-only, against 15 vs ~0 before. That residue is model
behaviour under a verbatim-copy contract, not scaffolding, and it is what
issue #6 should be narrowed to if kept open.

Dataset drift: 1 of 32 samples flagged (s20, unchanged from the earlier sweep).
