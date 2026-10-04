# Demo harnesses

Three harnesses that each solve a real task, end to end, with a real model.
Each folder is complete: a `harness.yaml`, the code it declares, its data, and
a README that states **what it proves**, how to run it, and what evidence to
look for. They were built — and are changed — only through the CLI (`init`,
`add`, `set`), never by editing `harness.yaml` by hand.

| demo | the job | what it proves |
|---|---|---|
| [ticket-triage](ticket-triage) | triage a 26-ticket support queue read from an MCP ticket system | an external MCP server as the only data source, reads fanned out in parallel, and a report checked against the system of record — every open ticket once, no invented ids |
| [ranked-retrieval](ranked-retrieval) | answer engineering questions from a 25-record knowledge base, in the words people actually use | enforced phases, a verify-first search tool and grounded ids keep a small model honest; a local eval measures recall and nDCG, and a research program attacks the vocabulary gap with measured experiments |
| [log-forensics](log-forensics) | find the dominant failure, error count and build digest in a 77 KB production log | a confined, allowlisted shell whose output is larger than the context, spilled whole and read back by handle; a second run recalls the first |

All three need an API key for their provider (`ANTHROPIC_API_KEY` by default;
any provider works through `hiveloom set model provider/model-id`). On a small
model a run costs well under a cent.

## In the workbench

`devtools/ui/dev.sh --showcase` seeds copies of the three on a real model —
runs, a fork resumed from its report turn, an eval, a measured evolution round
and a research program waiting for review — so every tab of the workbench has
something real to show. See [devtools/ui/showcase.py](../devtools/ui/showcase.py).

## By capability

| capability | where to see it |
|---|---|
| journal, version hash, `stats`, `trace --verify` | all three |
| guardrails: cost, wall clock | all three |
| MCP servers as tools | [ticket-triage](ticket-triage) |
| loop: parallel tool execution | [ticket-triage](ticket-triage) |
| verification: code validators against the system of record | [ticket-triage](ticket-triage) |
| forking a run and `--resume` | [ticket-triage](ticket-triage) (any run of any harness) |
| custom `@tool` | [ranked-retrieval](ranked-retrieval) |
| loop: `sequential_steps` | [ranked-retrieval](ranked-retrieval), [log-forensics](log-forensics) |
| verification: `output_schema`, `grounded_references` | [ranked-retrieval](ranked-retrieval) |
| evals, datasets, scorers, metric objectives | [ranked-retrieval](ranked-retrieval) |
| `evolve --experiment`, `hiveloom assess` | [ranked-retrieval](ranked-retrieval) |
| research programs (`hiveloom research`) | [ranked-retrieval](ranked-retrieval) |
| confinement of spawned processes, provider egress screening | [log-forensics](log-forensics) |
| spill, handles, `search_tool_result` / `read_tool_result` | [log-forensics](log-forensics) |
| `recall_runs` | [log-forensics](log-forensics) |

## More worked examples

The test suite keeps further harnesses in
[`tests/fixtures/harnesses/`](../tests/fixtures/harnesses/). Most run offline on
a scripted provider (no API key, the same journal every time), which is why the
tests and CI drive them; they are good reading for capabilities the three
demos above do not exercise:

| example | shows |
|---|---|
| [quickstart](../tests/fixtures/harnesses/quickstart) | the minimal harness: no tools, redaction, `regex_output_filter`, `max_turns_hard_cap` |
| [example-summarizer](../tests/fixtures/harnesses/example-summarizer) | skills and `load_skill`, schema plus code verification |
| [article-extractor](../tests/fixtures/harnesses/article-extractor) | a validator that re-fetches the page to catch invention (the subject of `evals/article-extractor`) |
| [routing-lab](../tests/fixtures/harnesses/routing-lab) | playbooks, `plan_then_act`, aimed evolution — offline |
| [memory-lab](../tests/fixtures/harnesses/memory-lab) | `notes`, `transform_result`, `propose_memory`, `memory.entries` — offline |
| [signal-lab](../tests/fixtures/harnesses/signal-lab) | `hiveloom signal`, reflection, auto-propose, relevance-selected memory — offline |
| [delegation-lab](../tests/fixtures/harnesses/delegation-lab) | delegation between harnesses, referrals, lineage — offline |
| [research-lab](../tests/fixtures/harnesses/research-lab) | a research program with a scripted director, start to promotion — offline |

## Verified

`scripts/package_e2e.py` builds the wheel, installs it in a clean environment,
and drives every offline example end to end, validating and dry-running the
live ones (a CI step). With `--live` and an OpenRouter key it also runs the
live harnesses on small models.
