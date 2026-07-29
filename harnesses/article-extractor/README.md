# article-extractor

Fetch a single article or blog page by URL and extract structured metadata into strict JSON.

## What this is

A self-contained **hiveloom** harness. The folder is the harness; the runtime
(`pip install hiveloom`) executes it.

## Run

```bash
cp .env.example .env   # fill in ANTHROPIC_API_KEY
hiveloom run . --input path/to/input.txt
```

Traces are written to `.hiveloom/traces/` and travel with the harness.
