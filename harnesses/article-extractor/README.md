# article-extractor

URL in, strict JSON metadata out: `source_url`, `title`, `description`,
`author`, `published_date`, `headings`.

This is the harness to read for **custom tools** and **anti-hallucination
verification**.

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

## Changing it

Do not hand-edit `harness.yaml`. Make changes through the CLI — `hiveloom
set`, `hiveloom add`, `hiveloom remove` — which validates every mutation and
rolls back on error.
