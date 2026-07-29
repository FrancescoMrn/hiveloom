# Results: article-extractor benchmark

Generated 2026-07-29 · dataset `uncommitted` · repo `ffbe54c`

| Arm | n | Task success (95% CI) | Halluc. | Title | Author | Date | Headings F1 | Mean cost | Cost/success | p50/p90 lat (s) | pass^k |
|---|---|---|---|---|---|---|---|---|---|---|---|
| haiku_harness | 96 | 61% (51–71) | 11% | 63% | 99% | 89% | 0.89 | $0.0120 | $0.0196 | 10.7 / 16.4 | 59% |
| haiku_raw | 96 | 2% (1–7) | 98% | 2% | 3% | 2% | 0.02 | $0.0071 | $0.3405 | 6.6 / 11.5 | 0% |
| sonnet_raw | 96 | 99% (94–100) | 0% | 100% | 97% | 86% | 1.00 | $0.0076 | $0.0077 | 6.4 / 9.2 | 97% |
| qwen_harness | 96 | 64% (54–72) | 24% | 87% | 88% | 83% | 0.65 | $0.0000 | $0.0000 | 35.8 / 88.1 | 53% |

## Paired comparison

**haiku_harness vs sonnet_raw**: 96 paired runs; harness-only wins b=1, raw-only wins c=37; exact McNemar p=0.0000

**haiku_harness vs haiku_raw** (harness contribution): 96 paired runs; harness-only wins b=57, raw-only wins c=0; exact McNemar p=0.0000

## Reading notes

- **haiku title misses are systematic, not random**: Haiku strips site-name
  suffixes ("… [LWN.net]", "… - Ars Technica", "… \\ Anthropic") and
  normalizes typographic quotes instead of copying the TITLE line verbatim.
  Under the task's verbatim contract these are failures (and Sonnet scores 99%
  under the same rule), but with a lenient prefix-match title metric
  haiku_harness would land meaningfully higher. The on-page validator can't
  catch suffix-stripping when the stripped title also appears verbatim as the
  page H1 — only the golden comparison does.
- **s16 drifted after scoring**: the post-sweep drift check flags s16 (docs),
  but all arms scored it C on all epochs before the page changed — results are
  uncontaminated. Re-verify its golden before the next sweep.

## Caveats

- Pricing (USD/Mtok, as of 2026-07-29): claude-haiku-4-5 (1.0, 5.0), claude-sonnet-5 (2.0, 10.0). Sonnet 5 is at the introductory rate through 2026-08-31.
- qwen arm cost is ~$0 by hiveloom accounting (local inference, usage-less openai-compat responses counted free) — latency is its resource proxy.
- The scorer strips one markdown fence layer for all arms (lenient to raw arms).
- Harness-arm cost comes from hiveloom's own accounting; raw-arm cost from inspect_ai token usage × the pricing table (cache writes at 1.25x input, reads at 0.1x).
- A `*` on a cost cell means some rows in that arm had no price (unpriced model or timeout) and are excluded from the cost math.
- Live-URL dataset: see the drift check run alongside this sweep.

Logs: `logs/haiku_harness/2026-07-29T13-19-47-00-00_article-extractor-harness_kZF8TvtGFUPQJL4gLCwgs5.eval`, `logs/haiku_raw/2026-07-29T13-24-00-00-00_article-extractor-raw_2vhoers8Tnh83aFKrsBCnn.eval`, `logs/sonnet_raw/2026-07-29T13-27-22-00-00_article-extractor-raw_Emdqcb6GkhQM6GU9BkKHeJ.eval`, `logs/qwen_harness/2026-07-29T13-30-11-00-00_article-extractor-harness_M4KzC9exdpVrMEwbUz3Jei.eval`
