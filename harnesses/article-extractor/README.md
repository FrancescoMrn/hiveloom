# article-extractor

Fetch a single article or blog page by URL and extract structured metadata into strict JSON.

## What this is

A self-contained **hiveloom** harness. The folder is the harness; the runtime
(`pip install hiveloom`) executes it. The run input is the **URL to extract**;
`tools/fetch_clean.py` reduces the page to a labelled digest, and
`validators/article_on_page.py` re-fetches the live page to check that the
title and headings actually occur on it (anti-hallucination).

## How it was built

Every step below is validated and rolled back on error — the folder is never
hand-edited. `init` already supplies this harness's model block
(`claude-haiku-4-5`, 4096 tokens, temperature 0), the `max_cost_usd` guardrail,
the `react` loop with `require_verification`, and the
`retry_with_feedback` / `max_retries: 2` failure policy, so the steps below are
only the deltas:

```bash
hiveloom init ./article-extractor --name article-extractor \
  --task "Fetch a single article or blog page by URL and extract structured metadata into strict JSON."
hiveloom set system_prompt --file prompt.txt
hiveloom set context.max_input_tokens 100000
hiveloom set context.strategy full
hiveloom set loop.max_turns 10
hiveloom add tool --code tools/fetch_clean.py:fetch_clean \
  --description 'HTTP GET a page and return a compact deterministic digest: title, metadata, h1-h3 headings, lead text — always under 8KB.'
hiveloom add hook --on before_verification --builtin strip_json_fence
hiveloom add validator --builtin output_schema --schema-file ./schemas/output.json
hiveloom add validator --code validators/article_on_page.py:validate \
  --description 'source_url must equal the run input; title/headings must appear verbatim on the live page (anti-hallucination).'
hiveloom add guardrail --builtin no_network_write
hiveloom add guardrail --builtin max_wall_clock_seconds --value 240
hiveloom add guardrail --builtin tool_allowlist
hiveloom validate .
```

`--code` scaffolds a stub for a hook file that does not exist yet; here
`tools/fetch_clean.py` and `validators/article_on_page.py` are written first and
the command picks them up.

Why these settings: `context.strategy: full` with a 100k input budget because
the digest is already clipped under 8KB by the tool, so there is nothing worth
dropping from a short transcript; `loop.max_turns: 10` because the task is one
fetch plus one emit, and a model still looping after ten turns is stuck, not
working. Guardrails: cost capped at $1.00, 240 s wall clock, `no_network_write`
blocks any tool tagged both `network` and `write` (this harness only reads), and
`tool_allowlist` confines the run to the one declared tool. The
`strip_json_fence` `before_verification` hook is an explicit output normalizer:
it unwraps a response fenced as a single `json` code block before the schema and
anti-hallucination validators run. It does not extract JSON from prose or
otherwise weaken the output contract.

## Run

```bash
cp .env.example .env   # fill in ANTHROPIC_API_KEY
hiveloom run . --input "https://example.com/some-article" --json
```

Traces are written to `.hiveloom/traces/` and travel with the harness.

The shipped `model:` block is `claude-haiku-4-5`. To run the same harness on a
local model, register the provider in `~/.hiveloom/models.yaml` (see
[docs/extending.md](../../docs/extending.md)) and switch it with
`hiveloom set model '{"provider": "ollama", "id": "qwen3:4b-instruct", "max_tokens": 4096, "temperature": 0.0}' --dir .`
— never by hand-editing `harness.yaml`.
