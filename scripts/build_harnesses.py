#!/usr/bin/env python3
"""Build the demo harnesses from zero, through hiveloom's own construct API.

``harnesses/`` used to be hand-maintained YAML. That made it drift: specs
carried fields the loader had since renamed, defaults that no longer matched
what ``hiveloom init`` produces, and evolution markers left over from runs
nobody remembers. A demo harness that does not match what the tool builds
today is worse than no demo at all.

So this script *is* the harnesses. Every spec here is produced by
``init_harness`` followed by ``add_*``/``set_*`` calls — the same validated,
rolled-back-on-error path a person gets from ``hiveloom init`` and
``hiveloom add``. Nothing writes ``harness.yaml`` directly. Re-run it after a
schema change and the demos come back correct, or the build fails where the
API refused, which is the same signal.

The set is curated rather than exhaustive: four harnesses, each the smallest
thing that shows one layer of the runtime.

* ``quickstart``        — a harness with no tools at all: prompt, guardrails, a
                          run, a trace.
* ``example-summarizer``— builtin tools, a JSON-schema check and a code
                          validator, and the retry-with-feedback loop.
* ``article-extractor`` — a custom ``@tool``, an output hook, and a validator
                          that re-fetches the page to catch invention.
* ``routing-lab``       — playbooks that move the model *and* the tool set
                          mid-run, on an offline provider so the fork/evolve
                          walkthrough needs no API key.

Usage::

    python scripts/build_harnesses.py                 # rebuild into harnesses/
    python scripts/build_harnesses.py --only quickstart
    python scripts/build_harnesses.py --no-archive    # delete instead of moving
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from hiveloom import construct  # noqa: E402
from hiveloom import fork as fork_mod  # noqa: E402

HIVELOOM = str(REPO / ".venv" / "bin" / "hiveloom")


# --------------------------------------------------------------------------- #
# Assets
# --------------------------------------------------------------------------- #
# Code and schemas live here as text rather than being copied from the old
# folders, so a rebuild depends on nothing but this file.

CHECK_SUMMARY = '''\
"""Code validator for example-summarizer: is this a real summary?

The JSON-schema check beside it proves the *shape*. This proves the two things
a schema cannot express: that the fields carry content, and that the summary is
actually shorter than what it summarised. Feedback is written to be read by the
model on retry, so each message says what to change rather than what was wrong.
"""

from __future__ import annotations

import json
from typing import Any


def validate(run_output: str, run_context: dict[str, Any]) -> dict[str, Any]:
    """Return ``{"passed": bool, "feedback": str}`` for one run's output."""
    try:
        data = json.loads(run_output)
    except (json.JSONDecodeError, TypeError):
        return {
            "passed": False,
            "feedback": "Output is not valid JSON. Emit a single JSON object and nothing else.",
        }
    if not isinstance(data, dict):
        return {"passed": False, "feedback": "The top-level JSON value must be an object."}

    missing = [key for key in ("title", "summary", "key_points") if key not in data]
    if missing:
        return {"passed": False, "feedback": f"Add the missing key(s): {', '.join(missing)}."}

    if not isinstance(data["title"], str) or not data["title"].strip():
        return {"passed": False, "feedback": "'title' must be a non-empty string."}
    if not isinstance(data["summary"], str) or not data["summary"].strip():
        return {"passed": False, "feedback": "'summary' must be a non-empty string."}
    if not isinstance(data["key_points"], list) or not data["key_points"]:
        return {"passed": False, "feedback": "'key_points' must be a non-empty array of strings."}
    if any(not isinstance(point, str) or not point.strip() for point in data["key_points"]):
        return {
            "passed": False,
            "feedback": "Every entry in 'key_points' must be a non-empty string.",
        }

    # `input` is the run input: the path when the harness was given a file, so
    # the length check only applies once there is a source to compare against.
    source = str(run_context.get("input") or "")
    if source and len(data["summary"]) >= len(source):
        return {
            "passed": False,
            "feedback": (
                "The summary is not shorter than the source text. Condense it: "
                "keep the claims, drop the restatement."
            ),
        }
    return {"passed": True, "feedback": ""}
'''

SUMMARIZER_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "example-summarizer output",
    "type": "object",
    "required": ["title", "summary", "key_points"],
    "additionalProperties": False,
    "properties": {
        "title": {"type": "string", "minLength": 1},
        "summary": {"type": "string", "minLength": 1},
        "key_points": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string", "minLength": 1},
        },
    },
}

NOTES_TXT = """\
Harness engineering notes
=========================

A model is not an agent. What turns one into the other is everything around
the call: the tools it may reach for, the context it is shown, the loop that
decides whether to go again, and the check that decides whether the work is
done. That surrounding structure is the harness.

Treating the harness as a first-class artifact has three consequences. It can
be versioned, so a change to a prompt is a change you can point at. It can be
measured, because every run lands in a bucket keyed by the version that
produced it. And it can be changed on evidence rather than on recollection:
the failures are on record, so the next edit can be argued for.

The cost of not doing this is familiar. Prompts live in a chat window, a
change that helped is remembered rather than recorded, and a regression is
noticed weeks later with no way to say which edit caused it.
"""

ARTICLE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "article-extractor output",
    "type": "object",
    "required": [
        "source_url",
        "title",
        "description",
        "author",
        "published_date",
        "headings",
    ],
    "additionalProperties": False,
    "properties": {
        "source_url": {
            "type": "string",
            "pattern": "^https?://",
            "description": "Exactly the input URL, character for character.",
        },
        "title": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Page title (title tag, og:title, or first h1). Required and "
                "non-empty: a run without a title must fail."
            ),
        },
        "description": {
            "type": ["string", "null"],
            "description": "meta description or og:description; null if the page has none.",
        },
        "author": {
            "type": ["string", "null"],
            "description": "meta author, byline, or rel=author text; null if absent.",
        },
        "published_date": {
            "type": ["string", "null"],
            "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$",
            "description": (
                "Publication date normalized to YYYY-MM-DD; null if absent or "
                "not confidently normalizable."
            ),
        },
        "headings": {
            "type": "array",
            "maxItems": 15,
            "items": {"type": "string", "minLength": 1},
            "description": (
                "Text of h1/h2/h3 elements in document order, tags stripped; "
                "empty array if none."
            ),
        },
    },
}

DECISION_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "routing-lab decision",
    "type": "object",
    "required": ["severity", "owner", "action"],
    "additionalProperties": False,
    "properties": {
        "severity": {"type": "string", "enum": ["low", "medium", "high"]},
        "owner": {"type": "string", "minLength": 1},
        "action": {"type": "string", "minLength": 1},
    },
}

INCIDENT_TXT = """\
INCIDENT-4471  checkout-worker
================================

19:02  Queue depth on checkout-worker begins climbing from a steady ~200 to
       ~4,900 over eleven minutes.
19:06  p99 latency on POST /checkout crosses 30s; the client-side timeout is
       30s, so callers start seeing failures.
19:09  Worker process RSS flat. CPU flat at 40%. No restarts, no OOM kills.
19:13  Upstream payment provider reports normal latency from their side.
19:20  Depth still rising. No dropped messages: every enqueued job is still in
       the queue, so nothing has been lost — the workers are simply not
       draining it.

Deploys in the window: none. Config changes in the window: none.
"""

TRIAGE_PROMPT = """\
# Triage

You are collecting evidence, not deciding. Read `incident.txt` with the file
tool and establish, from the text alone:

- what is degrading, and since when;
- what has been ruled out already;
- whether anything is irreversible (data loss, corruption) or merely stalled.

Do not propose an owner, a severity, or a remedy while you are in this
playbook — you are on the cheap model precisely because this part is reading
rather than judging. As soon as the evidence above is on the table, call
`switch_playbook` for `decide` and say what you found.
"""

DECIDE_PROMPT = """\
# Decide

You have the evidence; now commit to a decision. You are on the larger model
and you have no file tool — everything you need is already in the
conversation, and re-reading it would only spend tokens re-deriving what the
triage step established.

Answer with exactly one JSON object:

- `severity`: `low`, `medium`, or `high`. Reserve `high` for irreversible or
  spreading damage; a stalled queue that has lost nothing is not `high`.
- `owner`: the team that owns the failing component.
- `action`: the single next step, concrete enough to carry out.

No prose before or after the object.
"""


def _asset(name: str) -> str:
    """Read one of the Python assets kept beside this script."""
    return (Path(__file__).resolve().parent / "harness_assets" / name).read_text(
        encoding="utf-8"
    )


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #
def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_json(path: Path, data: object) -> None:
    write(path, json.dumps(data, indent=2) + "\n")


def build_quickstart(directory: Path) -> None:
    """The smallest harness that is still a harness: no tools at all."""
    construct.init_harness(
        directory,
        name="quickstart",
        task="Answer the question you are given, in three sentences or fewer.",
    )
    construct.set_value(
        directory,
        "system_prompt",
        (
            "Answer the question you are given in three sentences or fewer.\n\n"
            "You have no tools. If the question needs information you do not "
            "have, say so plainly in one sentence instead of guessing — a "
            "harness with no tools cannot go and look, and an invented answer "
            "is worse than an admitted gap."
        ),
    )
    construct.set_model(directory, "claude/claude-haiku-4-5")
    construct.set_value(directory, "model.max_tokens", 1024)
    construct.set_value(directory, "model.temperature", 0.0)

    # A ceiling on a harness that cannot call anything is not about runaway
    # tool use — it is the habit: every harness declares what it may spend.
    construct.add_guardrail(directory, "max_cost_usd", 0.05)
    construct.add_guardrail(directory, "max_wall_clock_seconds", 60)

    construct.set_value(directory, "loop.max_turns", 2)
    # Nothing to verify and nothing to retry: the answer is the answer.
    construct.set_value(directory, "loop.require_verification", False)
    construct.set_value(directory, "context.max_input_tokens", 8000)

    write(
        directory / "README.md",
        """\
# quickstart

The smallest harness that is still a harness: a prompt, a model, two
guardrails, and no tools whatsoever.

It exists to make one point. Even here — where the harness adds no
capability — it adds a *record*. The run is journalled, the spec it ran under
is hashed, and the cost ceiling is declared in the file rather than hoped for.
Everything the other demos add is layered onto this.

## Run it

```bash
uv sync                       # install the pinned runtime
cp .env.example .env          # add ANTHROPIC_API_KEY
hiveloom validate .
hiveloom run . --input "What is a harness, in the hiveloom sense?"
```

## Then look at what happened

```bash
hiveloom stats .              # success rate, cost and turns, per version hash
hiveloom trace <run_id>       # the ordered journal for one run
```

## Where to go next

- `../example-summarizer` adds tools and verification.
- `../article-extractor` adds a custom tool written in Python.
- `../routing-lab` adds playbooks, per-playbook models, and forking.

## Rebuilding

This folder is generated. Edit `scripts/build_harnesses.py` and re-run it
rather than editing `harness.yaml` by hand.
""",
    )


def build_summarizer(directory: Path) -> None:
    """Builtin tools, two-layer verification, retry with feedback."""
    construct.init_harness(
        directory,
        name="example-summarizer",
        task="Summarize a text file into a structured JSON summary.",
    )
    construct.set_value(
        directory,
        "system_prompt",
        (
            "You summarize a source text into one compact, structured JSON "
            "object.\n\n"
            "Read the file named in the task input with the `file_read` tool, "
            "then emit exactly one JSON object with these keys:\n\n"
            '  "title"       a short title for the source\n'
            '  "summary"     a condensed prose summary, shorter than the source\n'
            '  "key_points"  an array of the claims the source actually makes\n\n'
            "Rules:\n"
            "- Emit the JSON object and nothing else — no prose, no markdown "
            "fences, no backticks.\n"
            "- Summarize what the source says. Do not add claims it does not "
            "make, and do not editorialise about it.\n"
            "- If verification comes back with feedback, fix exactly what the "
            "feedback names and re-emit the whole object."
        ),
    )
    construct.set_model(directory, "claude/claude-haiku-4-5")
    construct.set_value(directory, "model.max_tokens", 4096)
    construct.set_value(directory, "model.temperature", 0.0)

    construct.add_tool(directory, builtin="file_read")
    construct.add_tool(directory, builtin="file_write")

    construct.add_guardrail(directory, "max_cost_usd", 0.25)
    construct.add_guardrail(directory, "max_wall_clock_seconds", 120)
    # With an allowlist in force the model can only reach the two tools above;
    # a tool that arrives by any other route is refused rather than run.
    construct.add_guardrail(directory, "tool_allowlist")

    write_json(directory / "schemas" / "output.json", SUMMARIZER_SCHEMA)
    write(directory / "validators" / "check_summary.py", CHECK_SUMMARY)
    write(directory / "notes.txt", NOTES_TXT)

    # Shape first, then meaning: the schema rejects a malformed object before
    # the code validator has to defend against one.
    construct.add_validator(
        directory, builtin="output_schema", schema_file="./schemas/output.json"
    )
    construct.add_validator(
        directory,
        code="validators/check_summary.py:validate",
        description="The summary must carry content and be shorter than the source.",
    )
    construct.set_value(directory, "verify.on_fail.action", "retry_with_feedback")
    construct.set_value(directory, "verify.on_fail.max_retries", 2)

    construct.set_value(directory, "loop.max_turns", 12)
    construct.set_value(directory, "context.max_input_tokens", 30000)
    construct.set_value(directory, "logging.level", "full")
    construct.set_value(directory, "logging.redact", ["api[_-]?key"])

    write(
        directory / "README.md",
        """\
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
""",
    )


def build_article_extractor(directory: Path) -> None:
    """A custom Python tool, an output hook, and a validator that re-checks."""
    construct.init_harness(
        directory,
        name="article-extractor",
        task=(
            "Fetch a single article or blog page by URL and extract structured "
            "metadata into strict JSON."
        ),
    )
    construct.set_value(directory, "system_prompt", ARTICLE_PROMPT)
    construct.set_model(directory, "claude/claude-haiku-4-5")
    construct.set_value(directory, "model.max_tokens", 4096)
    construct.set_value(directory, "model.temperature", 0.0)

    write(directory / "tools" / "fetch_clean.py", _asset("fetch_clean.py"))
    write(directory / "validators" / "article_on_page.py", _asset("article_on_page.py"))
    write_json(directory / "schemas" / "output.json", ARTICLE_SCHEMA)

    construct.add_tool(
        directory,
        code="tools/fetch_clean.py:fetch_clean",
        description=(
            "HTTP GET a page and return a compact deterministic digest: title, "
            "metadata, h1-h3 headings, lead text — always under 8KB."
        ),
    )
    # The model is told not to fence its JSON, and mostly does not. The hook is
    # for the residue: strip the fence before verification rather than fail a
    # run whose only fault is three backticks.
    construct.add_hook(directory, on="before_verification", builtin="strip_json_fence")

    construct.add_guardrail(directory, "max_cost_usd", 1.0)
    construct.add_guardrail(directory, "max_wall_clock_seconds", 240)
    construct.add_guardrail(directory, "no_network_write")
    construct.add_guardrail(directory, "tool_allowlist")

    construct.add_validator(
        directory, builtin="output_schema", schema_file="./schemas/output.json"
    )
    construct.add_validator(
        directory,
        code="validators/article_on_page.py:validate",
        description=(
            "source_url must equal the run input; title and headings must appear "
            "verbatim on the live page (anti-hallucination)."
        ),
    )
    construct.set_value(directory, "verify.on_fail.action", "retry_with_feedback")
    construct.set_value(directory, "verify.on_fail.max_retries", 2)

    construct.set_value(directory, "loop.max_turns", 10)
    # `full` rather than `rolling`: the digest is bounded by the tool itself, so
    # there is nothing to compact and dropping a turn would lose the evidence
    # the validator checks the answer against.
    construct.set_value(directory, "context.strategy", "full")
    construct.set_value(directory, "context.max_input_tokens", 100000)
    construct.set_value(directory, "logging.level", "full")

    write(
        directory / "README.md",
        """\
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

## Rebuilding

This folder is generated. Edit `scripts/build_harnesses.py` and re-run it
rather than editing `harness.yaml` by hand. The two Python assets live in
`scripts/harness_assets/`.
""",
    )


def build_routing_lab(directory: Path) -> None:
    """Playbooks that move the model and the tool set mid-run — offline."""
    construct.init_harness(
        directory,
        name="routing-lab",
        task=(
            "Read an incident, triage it on a cheap model, then route to a "
            "decision model and emit a verified JSON decision."
        ),
    )
    construct.set_value(
        directory,
        "system_prompt",
        (
            "You handle one incident, in two stages.\n\n"
            "First `triage`: read the incident file and establish what is "
            "actually happening. Then `decide`: commit to a severity, an owner "
            "and a next action, as exactly one JSON object.\n\n"
            "The active playbook tells you which stage you are in and what you "
            "may use there. Finish with the JSON object and nothing else."
        ),
    )

    # The provider is an extension of this harness, not a runtime builtin: the
    # walkthrough has to be reproducible and offline, and a scripted provider
    # scoped to one folder is how that is done without pretending it is a
    # model anyone should ship against.
    write(directory / "extensions" / "qa_provider.py", _asset("qa_provider.py"))
    construct.set_value(directory, "extensions", ["extensions/qa_provider.py"])
    construct.set_model(directory, "routing_lab/qa-triage")
    construct.set_value(directory, "model.max_tokens", 4096)

    construct.add_tool(directory, builtin="file_read")
    construct.add_guardrail(directory, "max_cost_usd", 1.0)

    write(directory / "incident.txt", INCIDENT_TXT)
    write(directory / "playbooks" / "triage.md", TRIAGE_PROMPT)
    write(directory / "playbooks" / "decide.md", DECIDE_PROMPT)
    write_json(directory / "schemas" / "decision.json", DECISION_SCHEMA)

    # Both the model and the tool set move at the playbook boundary: `triage`
    # reads on the cheap model, `decide` has no file tool at all and runs on
    # the larger one. That the tool list shrinks is the load-bearing part —
    # routing that only changed the model would be a model swap.
    construct.add_playbook(
        directory,
        name="triage",
        description="Collect incident evidence with the inexpensive model.",
        prompt="playbooks/triage.md",
        tools=["file_read", "switch_playbook"],
        entry=True,
    )
    construct.add_playbook(
        directory,
        name="decide",
        description="Turn collected evidence into the final verified decision.",
        prompt="playbooks/decide.md",
        tools=["switch_playbook"],
        model="qa-decision",
        model_provider="routing_lab",
    )

    construct.add_validator(
        directory, builtin="output_schema", schema_file="./schemas/decision.json"
    )
    construct.set_value(directory, "verify.on_fail.action", "retry_with_feedback")
    construct.set_value(directory, "verify.on_fail.max_retries", 2)
    construct.set_value(directory, "loop.max_turns", 8)

    write(
        directory / "README.md",
        """\
# routing-lab

One incident, two stages, two models — and no API key required.

`extensions/qa_provider.py` registers a small scripted provider, so every
command below is deterministic and offline. That is what makes this the
harness to learn **playbooks**, **forking** and **evolution** on: you can run
the whole walkthrough repeatedly and get the same journal every time.

## What routing means here

| playbook | model         | tools                     |
|----------|---------------|---------------------------|
| `triage` | `qa-triage`   | `file_read`, `switch_playbook` |
| `decide` | `qa-decision` | `switch_playbook`         |

Both the executor *and* the tool set change at the boundary, inside one run
and one conversation. `decide` genuinely cannot read a file — the evidence it
reasons over is what `triage` already put in the conversation. Routing that
only swapped the model would be a model swap; this is a change of what the
agent *is* for that stretch of the run.

## Run it

```bash
uv sync                              # install the pinned runtime
hiveloom validate .
hiveloom run . --input incident.txt --json
hiveloom trace <run_id>              # watch the playbook switch land
```

## Fork it

A fork re-enters a finished run at one of its model calls and replays the
identical prefix against a changed harness — the same failure from the turn
where it went wrong, not a fresh run that may not reproduce it.

```bash
hiveloom fork <run_id> --list                    # the model calls you may re-enter
hiveloom fork <run_id> --at <seq> --name probe
```

The fork lands in **`.hiveloom/forks/probe`**, inside this harness. That is
deliberate: a fork is an experiment *on* this harness rather than a harness of
its own, so it belongs in this folder's workbench state. Archiving this folder
takes its experiments with it, a directory of harnesses stays a directory of
harnesses, and `file_read` above — rooted here, and not descending into
`.hiveloom` — cannot reach the experiment. The workbench shows forks nested
under the harness that contains them.

Edit the fork's `harness.yaml`, then:

```bash
hiveloom run .hiveloom/forks/probe --resume
hiveloom lineage <run_id>            # parent and forks, on their shared prefix
```

`--model` makes the commonest edit at fork time in one step — replay this
exact prefix on a different model:

```bash
hiveloom fork <run_id> --name on-alt --model qa-alt --provider routing_lab
```

## Rebuilding

This folder is generated. Edit `scripts/build_harnesses.py` and re-run it
rather than editing `harness.yaml` by hand. The provider lives in
`scripts/harness_assets/qa_provider.py`.
""",
    )


ARTICLE_PROMPT = """\
You extract structured metadata from one article or blog page. The task input \
is the URL to fetch.

## CRITICAL: YOUR FINAL MESSAGE MUST BE A NON-EMPTY JSON OBJECT
You MUST output exactly one JSON object as your final message — nothing else.
Never output an empty message.

## STEP-BY-STEP PROCEDURE

1. FETCH: Call fetch_clean with the exact URL from the task input RIGHT NOW, \
before doing anything else.
   The tool returns a labeled digest of the page, one item per line:
   TITLE / META <key> / TIME / H1 / H2 / H3 / LEAD TEXT / TAIL TEXT.
   - If the result starts with "ERROR:", call the tool once more. If it fails \
again, output exactly:
     {"source_url": "<the input URL>", "title": null, "description": null, \
"author": null, "published_date": null, "headings": []}
     and stop. This signals a failed scrape.

2. BUILD each field from the digest lines ONLY (first match wins):
   - title: the TITLE line; else META og:title; else the first H1 line.
   - description: META description; else META og:description; null if neither \
line exists.
   - author: META author or META article:author; else a clear byline or \
signature in LEAD TEXT or TAIL TEXT (e.g. "by Jane Doe", "Cheers, Jane Doe"); \
null if absent.
   - published_date: META article:published_time, or a TIME line, or a date \
visible in LEAD TEXT or TAIL TEXT, normalized to YYYY-MM-DD (e.g. "Jul 6, \
2026" becomes "2026-07-06"). Use null if absent or if you cannot normalize it \
with confidence.
   - headings: every H1, H2, and H3 line in the order given, text copied \
VERBATIM (everything after the "H1: "/"H2: "/"H3: " prefix). At most 15 — if \
there are more, keep the first 15. Empty array if there are none.
   - source_url: EXACTLY the URL from the task input, character for character.

3. VERIFY (anti-hallucination): every value must be copied \
character-for-character from a digest line.
   - NEVER invent, paraphrase, or add headings that are not H1/H2/H3 lines in \
the digest.
   - If a field has no matching digest line, use null (or the empty array for \
headings).

## MANDATORY PRE-OUTPUT CHECKLIST
Before sending your final message, answer each question:
- Did I call fetch_clean in this session and receive a digest? (If NO — call \
it now, do not output anything yet.)
- Is source_url exactly the input URL? (If NO — fix it.)
- Is every heading copied verbatim from an H1/H2/H3 digest line? (If NO — \
remove the offending entries.)
- Is published_date either null or exactly YYYY-MM-DD? (If NO — set it to null.)
- Does my output contain exactly these six keys and no others: source_url, \
title, description, author, published_date, headings? (If NO — fix it.)
- Does my final message start with '{' and end with '}' with zero characters \
outside? No markdown fences, no backticks, no prose? (If NO — fix it.)

## OUTPUT RULES (STRICT)
- Your final message MUST be a single raw JSON object and absolutely nothing \
else.
- Do NOT include explanations, markdown fences (```), code blocks, or \
backticks.
- Use null for unknown description/author/published_date. source_url is never \
null.
- title may be null ONLY in the failed-fetch fallback above.
"""


BUILDERS = {
    "quickstart": build_quickstart,
    "example-summarizer": build_summarizer,
    "article-extractor": build_article_extractor,
    "routing-lab": build_routing_lab,
}


# --------------------------------------------------------------------------- #
# Demo fork
# --------------------------------------------------------------------------- #
def seed_routing_lab_fork(directory: Path) -> str:
    """Run routing-lab offline and fork it, so the demo ships a real fork.

    Through the CLI rather than the library: this is the path a reader will
    take, so it is the path worth proving. The assertion below is the whole
    containment invariant — a fork of this harness is inside this harness.
    """
    run = subprocess.run(
        [HIVELOOM, "run", str(directory), "--input", "incident.txt", "--json"],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    if run.returncode != 0:
        raise SystemExit(f"routing-lab did not run:\n{run.stdout}\n{run.stderr}")
    run_id = json.loads(run.stdout)["run_id"]

    # The fork point is asked for rather than hardcoded: a seq is a position in
    # a journal, and pinning one here would break the moment the loop emits an
    # event more or fewer. The last model call is the decision turn — the one
    # worth replaying against a different model.
    listed = subprocess.run(
        [HIVELOOM, "fork", run_id, "--list", "--json"],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    if listed.returncode != 0:
        raise SystemExit(f"could not list fork points:\n{listed.stdout}\n{listed.stderr}")
    points = json.loads(listed.stdout)["fork_points"]
    if not points:
        raise SystemExit("the routing-lab run made no model calls, so it cannot be forked")
    at = str(points[-1]["seq"])

    forked = subprocess.run(
        [HIVELOOM, "fork", run_id, "--at", at, "--name", "decide-on-alt",
         "--model", "qa-alt", "--provider", "routing_lab", "--json"],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    if forked.returncode != 0:
        raise SystemExit(f"fork failed:\n{forked.stdout}\n{forked.stderr}")
    where = Path(json.loads(forked.stdout)["directory"]).resolve()

    expected = fork_mod.forks_dir(directory).resolve()
    if where.parent != expected:
        raise SystemExit(f"fork landed outside its harness: {where} (expected under {expected})")
    return str(where.relative_to(REPO))


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(REPO / "harnesses"))
    parser.add_argument("--only", action="append", choices=sorted(BUILDERS))
    parser.add_argument(
        "--no-archive",
        action="store_true",
        help="Delete an existing folder instead of moving it under .archive/.",
    )
    parser.add_argument(
        "--no-fork",
        action="store_true",
        help="Skip the routing-lab run that seeds its demo fork.",
    )
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    wanted = args.only or sorted(BUILDERS)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")

    for name in wanted:
        directory = out / name
        if directory.exists():
            if args.no_archive:
                shutil.rmtree(directory)
                print(f"  removed  {directory.relative_to(REPO)}")
            else:
                # Moved, not deleted: the folder holds journals of real runs,
                # and a rebuild is not a reason to lose the evidence.
                archive = REPO / ".archive" / f"harnesses-{stamp}" / name
                archive.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(directory), str(archive))
                print(f"  archived {directory.relative_to(REPO)} -> {archive.relative_to(REPO)}")
        BUILDERS[name](directory)
        print(f"  built    {directory.relative_to(REPO)}")

    if "routing-lab" in wanted and not args.no_fork:
        where = seed_routing_lab_fork(out / "routing-lab")
        print(f"  forked   {where}")

    print(f"\n{len(wanted)} harness(es) built. Validate them with:")
    for name in wanted:
        print(f"  hiveloom validate {(out / name).relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
