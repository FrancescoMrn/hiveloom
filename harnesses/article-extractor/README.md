# article-extractor

Fetch a single article or blog page by URL and extract structured metadata into strict JSON.

## What this is

A self-contained **hiveloom** harness. The folder is the harness; the runtime
(`pip install hiveloom`) executes it. The run input is the **URL to extract**;
`tools/fetch_clean.py` reduces the page to a labelled digest, and
`validators/article_on_page.py` re-fetches the live page to check that the
title and headings actually occur on it (anti-hallucination).

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
