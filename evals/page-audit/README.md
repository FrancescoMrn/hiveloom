# page-audit: exhaustiveness + arithmetic probe (Opus 5 / Sonnet 5)

Where the article-digest probe measured verbatim copying (frontier models:
0% fabrication), this one targets the failure modes frontier models still
have: **exhaustiveness beyond a truncated tool view, aggregation, and date
arithmetic**.

## Task

URL → strict JSON:

- `h2_count` — the page's TOTAL number of H2 headings,
- `h2_headings` — ALL of them, verbatim, in order,
- `published_date` — YYYY-MM-DD actually declared on the page (null if none;
  "never guess a date from memory"),
- `days_to_2026` — exact signed day count to 2026-01-01 (recomputed in code),
- `source_url` — exact input.

The trap is structural: `fetch_clean` clips at 30 headings / 7.5KB, and three
of the six pages (`s15` bash manual 15 H2s→7 shown, `s22` Cloudflare 15→10,
`s14` Postgres 7→6) have more H2s than the digest shows (calibrated by
`scripts/preflight.py`). Controls: `s09` complete, `s25` date arithmetic,
`s01` null-date path.

## Arms and scoring

Same design as `../article-digest`: 4 arms (opus/sonnet × harness/raw), raw =
validators removed + `require_verification false`, everything else identical.
All arms scored by `validators-src/page_audit.py:check` against an uncapped
live re-fetch. Headline metric: **silently wrong** (run reported success,
audit is wrong) vs **flagged** (run exited `verify_failed`).

## Run

```bash
cp ../article-digest/.env .env            # ANTHROPIC_API_KEY
./scripts/setup_harnesses.sh
python3 scripts/preflight.py              # optional: re-check truncation truth
python3 scripts/run_eval.py               # 24 runs, resumable
python3 scripts/score.py                  # writes RESULTS.md
```

## Caveats

- 6 URLs × 1 epoch is a probe: shapes of failure are the finding, not rates.
- Retry recovery on truncated pages works because the validator's feedback
  names the true count and example missing headings, and the page body text
  (table of contents in LEAD TEXT) plus the models' own knowledge lets them
  reconstruct the verbatim list — which the validator then re-verifies against
  the live page. On pages with no recoverable signal, retries exhaust and the
  run fails loudly instead.
- Needs the same two `models/claude.py` fixes and `~/.hiveloom/models.yaml`
  entries as `../article-digest` (see its README).
