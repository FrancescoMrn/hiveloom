# example-summarizer

> **Proves:** a harness can hold a model to a contract — shape, content and a
> house style it loads on demand — and send it back with actionable feedback
> until the output earns a success, rather than accepting the first reply.

Summarizes a text into one JSON object with `title`, `summary` and
`key_points`, written in the house style.

## Capabilities

- **Two independent verifications** — `schemas/output.json` checks the
  *shape*; `validators/check_summary.py` checks the *content*: fields carry
  something, the summary is shorter than its source, and the house style's
  countable rules hold. ([spec.md](../../docs/spec.md))
- **Retry with feedback** — a failed check puts the validator's own message
  back into the conversation, up to twice. The messages say what to change.
- **Skills, loaded on demand** — `skills/house-style/SKILL.md` stays out of the
  prompt; only its one-line description is indexed, and the model reads it with
  the `load_skill` tool when it needs it.
- **Builtin tools and a tool allowlist** — `file_read`, `file_write`,
  `load_skill`; `tool_allowlist` refuses anything else.

## Run it

Live: needs `ANTHROPIC_API_KEY` in `.env` (or any provider via
`--provider/--model`). A run costs well under a cent.

```bash
uv sync                       # install the pinned runtime
cp .env.example .env          # add ANTHROPIC_API_KEY
hiveloom validate .
hiveloom run . --input notes.txt --json
```

## What to look for

- **The skill is read, not pasted.** The trace's first `tool_call` is
  `load_skill` for `house-style`; the system prompt carries only the skill's
  description.
- **Verification that bites.** Each `verification_result` is journalled;
  when one fails, the next user turn is its feedback, and the model's next
  answer fixes exactly that.
- **The output** — a title of at most 8 words and 3–5 key points. Checked live
  on a small model (Ministral 8B): it loaded the skill and passed both checks
  first time.

## Try this

- Loosen the prompt and watch the checks catch it:
  `hiveloom set system_prompt "Summarize the file."`, run again, then
  `hiveloom stats .` — the two versions side by side.
- Change a house rule (say, "exactly 3 key points") in the skill and the
  validator, and see the retry loop teach it.
