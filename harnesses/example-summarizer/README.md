# example-summarizer

A complete, working hiveloom harness: it summarizes a text file into a
structured JSON object with `title`, `summary`, and `key_points`.

It runs the small executor model (`claude-haiku-4-5`) inside a harness that:

- gives it `file_read` / `file_write` tools (sandboxed to this folder),
- caps cost (`max_cost_usd: 0.25`) and wall-clock time, and allowlists tools,
- **verifies** the output two ways: a JSON-schema check (`schemas/output.json`)
  and a code hook (`validators/check_summary.py`) that also asserts the summary
  is shorter than the source. On failure, the loop retries with the validator's
  feedback injected.

## Run it

```bash
cp .env.example .env          # add ANTHROPIC_API_KEY
hiveloom validate .
hiveloom run . --input notes.txt --json
```

A sample `notes.txt` is included. Inspect the run afterwards:

```bash
hiveloom stats .              # success rate / cost / turns per version hash
hiveloom trace <run_id>       # the full ordered trace
```

## How it's tested

The repository's integration test drives this harness end to end with a
`FakeModelProvider` (scripted tool call + final answer) — so it exercises the
tools, guardrails, verification, retry-with-feedback, and trace emission with no
API key required.
