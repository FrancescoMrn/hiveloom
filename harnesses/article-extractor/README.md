# article-extractor

> **Proves:** code does the deterministic part and the model the judgement,
> and the answer is checked against the live page — a well-formed object full
> of invented headings fails the run.

URL in, strict JSON metadata out: `source_url`, `title`, `description`,
`author`, `published_date`, `headings`. Live: needs an API key and network.

## Capabilities

- **A custom `@tool`** — `tools/fetch_clean.py`, plain Python, returns a
  labelled digest that always fits the tool-result budget.
  ([extending.md](../../docs/extending.md))
- **Anti-hallucination verification** — `validators/article_on_page.py`
  re-fetches the page and checks the title and headings really occur on it.
- **Output hook** — `strip_json_fence` removes a Markdown fence before
  verification, so a formatting slip is not a failed run.
- **Guardrails** — `no_network_write` (the tool may read the web, never write
  to it) and `tool_allowlist`.

## The tool does the deterministic part

`tools/fetch_clean.py` is an ordinary Python function with a `@tool`
decorator. It fetches the page, parses it with the stdlib HTML parser, and
returns a labelled digest — `TITLE:`, `META …:`, `H1:`, `LEAD TEXT:` — that
always fits inside the runtime's tool-result clip.

That division is the point. Raw HTML would be truncated mid-body before the
model ever saw it, and the model would be doing string surgery on the
remainder. Parsing is something code is simply better at, so code does it; the
model is left with the part that needs judgement, which is mapping digest
lines onto schema fields.

## The validator does not trust the answer

`validators/article_on_page.py` re-fetches the page itself and checks that the
title and headings the model returned actually occur in it. A JSON schema will
happily accept a beautifully-formed object full of invented headings; this
will not. One missing heading is tolerated, because pages do change between
two fetches — more than one is fabrication, and the run fails with feedback
saying so.

## Run it

```bash
uv sync                       # install the pinned runtime
cp .env.example .env          # add ANTHROPIC_API_KEY
hiveloom validate .
hiveloom run . --input https://example.com/some-article --json
```

## What to look for

- One `fetch_clean` call, then the answer: the digest, not raw HTML, is what
  the model reasons over.
- `verification_result` from `article_on_page`: when a heading is not on the
  page, the feedback names it and the next answer drops or fixes it.

## Try this

- Run it on a page whose headings changed since it was cached anywhere: the
  validator re-fetches, so it judges the page as it is now.
- Remove the validator (`hiveloom remove validators/article_on_page.py:validate
  --dir .`) and compare how often headings are paraphrased rather than quoted.

Do not hand-edit `harness.yaml`; change it through `hiveloom set`/`add`/
`remove`, which validate every mutation and roll back on error.
