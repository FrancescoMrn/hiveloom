# hn-extractor

Fetch the Hacker News front page with `http_get` and extract the top stories
into structured JSON — a demo of a **website-extraction harness** built entirely
through the hiveloom construct CLI.

## What this is

A self-contained **hiveloom** harness. The folder is the harness; the runtime
(`pip install hiveloom` or `uv tool install hiveloom`) executes it. A small
executor model (`claude-haiku-4-5`) runs inside: it calls `http_get` on
`https://news.ycombinator.com/`, parses the story rows out of the HTML, and
emits a JSON object validated against [`schemas/output.json`](schemas/output.json):

```json
{
  "source": "https://news.ycombinator.com/",
  "fetched_stories": 10,
  "stories": [
    {"rank": 1, "title": "...", "url": "...", "points": 123, "comments": 45}
  ]
}
```

## How it was built

Every step below is validated and rolled back on error:

```bash
hiveloom init ./hn-extractor --name hn-extractor \
  --task "Fetch the Hacker News front page and extract the top stories into structured JSON."
hiveloom add tool --builtin http_get
hiveloom set system_prompt --file prompt.txt
hiveloom set loop.max_turns 8
hiveloom add validator --builtin output_schema --schema-file ./schemas/output.json
hiveloom add validator --builtin regex_match --pattern '"stories"'
hiveloom add validator --code validators/titles_on_page.py:validate \
  --description "Every extracted title must appear on the live HN front page (anti-hallucination)."
hiveloom add guardrail --builtin max_wall_clock_seconds --value 120
hiveloom add guardrail --builtin no_network_write
hiveloom validate .
```

Guardrails: cost capped at $0.25, 120 s wall clock, and `no_network_write`
blocks any tool tagged both `network` and `write` (this harness only reads).
The `strip_json_fence` `before_verification` hook is an explicit output
normalizer: it unwraps a response fenced as a single `json` code block before
the schema and anti-hallucination validators run. It does not extract JSON from
prose or otherwise weaken the output contract.

## Run

```bash
echo "ANTHROPIC_API_KEY=sk-..." > .env
hiveloom run . --input "Extract the top Hacker News stories." --json
```

No key? See the first call without any API use:

```bash
hiveloom run . --input "Extract the top Hacker News stories." --dry-run
```

## Package and deploy

For a released hiveloom version, package with `--docker` and build the emitted
Dockerfile. Before publishing hiveloom (or when it is available only through a
private index), embed the wheel built from this checkout instead:

```bash
uv build
hiveloom package . --docker \
  --runtime-wheel ../../dist/hiveloom-0.1.0-py3-none-any.whl \
  --output ../../dist
unzip ../../dist/hn-extractor-<version-hash>.zip -d /tmp/hn-artifact
docker build -t hn-extractor /tmp/hn-artifact/hn-extractor
docker run --rm -e ANTHROPIC_API_KEY \
  -v "$PWD/.hiveloom/traces:/harness/.hiveloom/traces" \
  hn-extractor --input "Extract the current top Hacker News stories."
```

The artifact's `.dockerignore` excludes `.env` and local trace memory; provide
credentials only with the runtime environment. The embedded wheel supplies
hiveloom itself; fully air-gapped builds also need a wheelhouse for dependencies.

## Evolution history (a real one)

This harness was evolved live against real failures. The first real run with
`claude-haiku-4-5` extracted 6 real stories and **hallucinated 4 plausible-looking
ones** — and passed the schema validator, because a schema can't smell a lie.
The `titles_on_page` code validator was added to cross-check every title against
the live page; runs then honestly failed, landing failure signatures in the Hive.
Four rounds of `hiveloom evolve` (strong model reads Hive failures → mutates
only fields in the mutable set) improved the context and prompt. A subsequent
explicit harness expansion added the conservative JSON-fence normalizer, so the
schema remains strict while a presentation-only model quirk does not waste a
verification retry. The harness returns only verifiable stories — fewer than
10 when that's all it can prove:

| version | result |
|---|---|
| `9c5c1122d35d` | hallucinated 4/10 stories, schema-blind "success" |
| `6aef13ed9b23` | + anti-hallucination validator → honest verify_failed |
| `7b1f653d4a9a` | evolve #1: verbatim-copy rules → still narrates prose |
| `2848f3bd8478` | evolve #2: output checklist → stops fabricating, still prose |
| `57e2d6856f5a` | evolve #3: parse-before-output discipline → **success** |
| `a52687f27ba7` | evolve #4: retain fetched HTML in full context; strengthen non-empty JSON fallback |
| `f9ba693c95bf` | + JSON-fence normalizer and count/rank contract → deployed artifact accepts presentation-only fences, then validates semantic output |

## Inspect memory

Traces land in `.hiveloom/traces/` and travel with the folder:

```bash
hiveloom stats .                     # success rate / cost / turns per version
hiveloom trace <run_id> --dir .      # ordered events of one run
hiveloom evolve .                    # after failures: propose a gated mutation
```
