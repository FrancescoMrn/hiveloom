# example-summarizer

Summarizes a text file into a structured JSON object with `title`, `summary`
and `key_points`.

This is the harness to read first if you want to see verification working. The
model gets two builtin tools and a cost ceiling, and its output has to survive
two independent checks before the run is allowed to succeed:

- **`schemas/output.json`** — a JSON-schema check on the *shape*: the three
  keys, the right types, no extras.
- **`validators/check_summary.py`** — a code check on the *content*: the
  fields carry something, and the summary is genuinely shorter than the source
  it summarised. A schema cannot express that second one.

When either fails, `retry_with_feedback` puts the validator's own message back
into the conversation and the loop goes again — up to twice. The feedback
strings are written to be read by a model, which is why they say what to
change rather than what was wrong.

## Run it

```bash
uv sync                       # install the pinned runtime
cp .env.example .env          # add ANTHROPIC_API_KEY
hiveloom validate .
hiveloom run . --input notes.txt --json
```

`notes.txt` ships with the harness, so the command above works as written.

## Watch verification bite

Loosen the prompt and the checks start failing where before they passed:

```bash
hiveloom set system_prompt "Summarize the file." && hiveloom run . --input notes.txt
hiveloom stats .              # the two versions, side by side
```

## Rebuilding

This folder is generated. Edit `scripts/build_harnesses.py` and re-run it
rather than editing `harness.yaml` by hand.
