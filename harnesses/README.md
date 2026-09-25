# Demo harnesses

Each folder is a complete harness: a `harness.yaml`, the code it declares, its
data, and a README that states **what it proves**, which capabilities it
exercises, how to run it, what evidence to look for, and what to try next. They
were built — and are changed — only through the CLI (`init`, `add`, `set`,
`memory`), never by editing `harness.yaml` by hand.

**Offline** demos run on a scripted provider shipped in their `extensions/`:
no API key, and the same journal every time. The scripts never carry the
answer — they react only to what they are actually shown (their prompt, their
tool results, the signal map), so the evidence is real. **Live** demos need an
API key for their provider (any provider works via `--provider/--model`).

| demo | proves | runs |
|---|---|---|
| [quickstart](quickstart) | a harness with no tools still adds a journal, a hashed spec, spending and turn ceilings, and a safety layer that keeps a credential out of the request, the trace and the answer | live |
| [example-summarizer](example-summarizer) | a model held to a contract — shape, content and a house style it loads as a skill — and retried with feedback until the output earns a success | live |
| [article-extractor](article-extractor) | code parses, the model judges, and the answer is checked against the live page so invented headings fail | live |
| [log-forensics](log-forensics) | an allowlisted, confined shell command produces more than the context holds and the harness still answers from its last line | live |
| [ranked-retrieval](ranked-retrieval) | structure beats model size: enforced phases, verify-first search and grounded ids make a 3B model reliable, measured by a local eval | live |
| [ticket-triage](ticket-triage) | an MCP server as the only data source, reads fanned out in parallel, one validated report with no invented ids | live |
| [routing-lab](routing-lab) | playbooks change the model *and* the tools mid-run on a pinned plan; forks re-enter a run at any call; an aimed evolution is confirmed by measurement | offline |
| [memory-lab](memory-lab) | a small executor works over more data than its context holds — narrowing it in place, keeping notes, exporting by handle — and learns only through review | offline |
| [signal-lab](signal-lab) | the harness finds where its failures come from, drafts an aimed fix on its own, and keeps a change only when its eval confirms it | offline |
| [delegation-lab](delegation-lab) | a harness hands a task to a peer only once the peer has earned it in measured runs, verifies the answer itself, and otherwise names the peer | offline |

## By capability

| capability | where to see it |
|---|---|
| journal, version hash, `stats` | every demo; [quickstart](quickstart) is the minimal case |
| guardrails: cost, wall clock | [quickstart](quickstart), [example-summarizer](example-summarizer), [article-extractor](article-extractor) |
| guardrails: `regex_output_filter`, `max_turns_hard_cap` | [quickstart](quickstart) |
| guardrails: `tool_allowlist` | [example-summarizer](example-summarizer), [article-extractor](article-extractor) |
| guardrails: `no_network_write` | [article-extractor](article-extractor) |
| redaction (`logging.redact`) | [quickstart](quickstart), [example-summarizer](example-summarizer) |
| provider egress screening | [quickstart](quickstart), [log-forensics](log-forensics) |
| confinement of spawned processes | [log-forensics](log-forensics) |
| verification: `output_schema`, retry with feedback | [example-summarizer](example-summarizer), [routing-lab](routing-lab), [memory-lab](memory-lab) |
| verification: code validators | [example-summarizer](example-summarizer), [article-extractor](article-extractor) |
| verification: `grounded_references` | [ranked-retrieval](ranked-retrieval) |
| verification: `regex_match` | [ticket-triage](ticket-triage), [signal-lab](signal-lab), [delegation-lab](delegation-lab) |
| verification: `file_exists`, `command_succeeds` | [memory-lab](memory-lab) |
| custom `@tool` | [article-extractor](article-extractor), [ranked-retrieval](ranked-retrieval), [signal-lab](signal-lab) |
| output hooks (`strip_json_fence`) | [article-extractor](article-extractor), [log-forensics](log-forensics), [ranked-retrieval](ranked-retrieval) |
| skills and `load_skill` | [example-summarizer](example-summarizer) |
| MCP servers as tools | [ticket-triage](ticket-triage) |
| serving a harness (`package`, `serve`, `mcp serve`) | [quickstart](quickstart), [delegation-lab](delegation-lab) |
| loop: `plan_then_act` | [routing-lab](routing-lab) |
| loop: `sequential_steps` | [ranked-retrieval](ranked-retrieval), [log-forensics](log-forensics) |
| loop: parallel tool execution | [ticket-triage](ticket-triage) |
| playbooks, per-playbook models | [routing-lab](routing-lab) |
| forking and `--resume` | [routing-lab](routing-lab), [memory-lab](memory-lab) |
| spill, handles, `transform_result` | [memory-lab](memory-lab), [log-forensics](log-forensics) |
| `notes` | [memory-lab](memory-lab) |
| `recall_runs` | [log-forensics](log-forensics) |
| durable memory: `memory.entries`, `propose_memory` | [memory-lab](memory-lab) |
| durable memory: relevance selection, `search_memory` | [signal-lab](signal-lab) |
| reflection (`evolution.reflect`) | [signal-lab](signal-lab) |
| `hiveloom signal` | [signal-lab](signal-lab), [routing-lab](routing-lab) |
| aimed proposals, `evolve --propose`, `proposals apply` | [routing-lab](routing-lab), [memory-lab](memory-lab), [signal-lab](signal-lab) |
| auto-propose with trace excerpts | [signal-lab](signal-lab) |
| `evolve --experiment`, `hiveloom assess` | [signal-lab](signal-lab) (offline), [ranked-retrieval](ranked-retrieval) (live) |
| evals, datasets, scorers, metric objectives | [ranked-retrieval](ranked-retrieval), [signal-lab](signal-lab) |
| delegation between harnesses, referrals, lineage | [delegation-lab](delegation-lab) |

`best_of_n` is experimental and not in a demo: its plurality vote needs
answers that can be compared verbatim, which none of these tasks produce.
`http_get` with pre-declared hosts is documented in
[spec.md](../docs/spec.md).

## Verified

`scripts/package_e2e.py` builds the wheel, installs it in a clean
environment, and drives every offline demo end to end, validating and
dry-running the live ones (a CI step). With `--live` and an OpenRouter key it
also runs the live demos on small models.
