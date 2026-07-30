# article-extractor evals

Does a small model inside a hiveloom harness match or beat a big model raw, at a
fraction of the cost? This benchmark tests hiveloom's core thesis on the
`article-extractor` task (URL → strict JSON metadata), built on
[inspect_ai](https://inspect.aisi.org.uk/).

## Arms

Each model runs raw (same system prompt + same `fetch_clean` tool, no
scaffolding) and, except Sonnet, inside the full hiveloom harness (validators,
retry-with-feedback, guardrails):

| Model | Arms | Served by |
|---|---|---|
| claude-haiku-4-5 | harness + raw | Anthropic API |
| claude-sonnet-5 | raw (incumbent baseline) | Anthropic API |
| qwen3:4b-instruct | harness + raw | native Ollama (Metal) |
| gemma4:12b-mlx | harness + raw | native Ollama (Metal) |
| mlx-community/Qwen3.6-35B-A3B-8bit | harness + raw | mlx_lm.server :8081 |

Local arms cost ~$0 by definition; latency is their resource proxy.

The raw arms reuse the harness's system prompt verbatim, so the eval measures
the **scaffolding delta** (validators / retries / guardrails / loop policy),
not prompt engineering.

## Metrics

- **task_success** — schema-valid AND no hallucination (title/headings verbatim
  on the re-fetched live page, via the harness's own
  `validators/article_on_page.py`) AND title matches golden. Wilson 95% CI.
- **cost_per_success** — total arm cost / successful samples (the headline
  economic number).
- Secondary: hallucination rate, per-field accuracy, headings F1, pass^3,
  p50/p90 latency. Paired McNemar test for haiku_harness vs sonnet_raw.

## Setup

```bash
uv sync
cp .env.example .env          # fill ANTHROPIC_API_KEY
ollama pull qwen3:4b-instruct
ollama pull gemma4:12b-mlx
python dataset/check_dataset_urls.py   # pre-flight: URL liveness + drift
```

The two local `ollama` arms need the native Ollama app on
`127.0.0.1:11434`; the `mlx` arm needs `mlx_lm.server` already serving
`mlx-community/Qwen3.6-35B-A3B-8bit` on `:8081` (`run_all_arms.sh` hard-fails
if it is not reachable). Both providers must be registered in
`~/.hiveloom/models.yaml` for the harness arms — see
[docs/extending.md](../../docs/extending.md).

The per-arm harness dirs under `harnesses/` are generated (not committed) from
the canonical `../../harnesses/article-extractor` by
`./scripts/setup_harnesses.sh` — run_all_arms.sh calls it automatically. Only
the `model:` block differs per arm (verify with
`diff harnesses/harness-haiku/harness.yaml harnesses/harness-qwen/harness.yaml`);
the script trusts each dir at build time and sets every arm's model explicitly,
so the canonical harness's own model never leaks into an arm.

## Run

```bash
./scripts/run_all_arms.sh              # all 9 arms, 3 epochs
python scripts/aggregate_results.py logs/* --out RESULTS.md
```

Or one arm at a time — see `scripts/run_all_arms.sh` for the individual
`inspect eval` invocations.

## Known caveats (disclosed in RESULTS.md)

- Live URLs can drift; every run stamps the dataset git hash and the pre-flight
  drift report. Goldens carry a `fingerprint` + `verified_at` for detection.
  The 2026-07-30 pre-flight found one known drift: `s20` kept the same article
  title and 14/15 headings, while GitHub Blog rotated the final related-post
  heading. Re-verify or replace that sample before the next scored sweep.
- The local arms report ~$0 cost (local inference has no API cost, and hiveloom
  counts usage-less openai-compat responses as free, so the number is not a
  measurement). Latency is their resource proxy.
- Sonnet 5 intro pricing ($2/$10 per Mtok) expires 2026-08-31; the pricing
  table used is dated in every report.
- The scorer strips one markdown fence layer for all arms (the harness has a
  `strip_json_fence` hook; raw arms don't) — slightly lenient to raw arms on a
  cosmetic formatting quirk.
