# The stateless worker

`hiveloom serve` runs one harness folder that lives on the server. Some
applications want the opposite: the harness lives **in the application** —
created there, stored version by version in its own database next to the data
it serves, its run journals and evolution proposals kept there too — and
Hiveloom only executes it. `hiveloom worker` is that executor.

```bash
HIVELOOM_API_KEY=... hiveloom worker --host 0.0.0.0 --port 8790
```

Every request carries the whole harness as a **bundle**,
`{"files": {"<relative path>": "<utf-8 text>"}}` with `harness.yaml` required.
The worker writes it to a fresh temporary directory, does the one job asked,
answers, and deletes the directory. It keeps no Hive, no trace and no trust
decision between requests; its `HIVELOOM_HOME` is a private temporary
directory, so trust recorded for its temporary harnesses never reaches the
machine's store.

A bundle arrives over the network, so it may hold no code: no `.py` file, no
`pyproject.toml`, no code tools or validators, no hooks, extensions or stdio
MCP servers, no `shell` tool (`hiveloom.serve.bundle.check_bundle`). Builtin
tools and validators, HTTP MCP servers, skills and schemas are what remains,
and they are enough: an application checks an answer with
[`http_verdict`](spec.md) and gives its tools through an HTTP MCP server whose
credential travels per run (`header_context`).

| Endpoint | Body | Answer |
|---|---|---|
| `GET /healthz` | — | `{ok, hiveloom_version}`, unauthenticated |
| `POST /harness/create` | `{name, task, ops?}` | `{bundle, version_hash}` |
| `POST /harness/construct` | `{bundle, ops}` | `{bundle, version_hash}`; an invalid op is a 422 and nothing applies |
| `POST /harness/validate` | `{bundle}` | `{ok, errors, version_hash}` |
| `POST /runs` | `{bundle, input \| messages, context?, stream?}` | the run's result with `journal` and `version_hash`; with `stream`, NDJSON frames ending on `run_result` |
| `POST /evolve` | `{bundle, journals, notes?, reflect?}` | `{proposals, note}` — never applied |
| `POST /proposals/apply` | `{bundle, proposal}` | `{bundle, version_hash, changed}`, re-gated; code changes refused |

Ops are the construction API over HTTP: `{"op": "set", "path", "value"}`
(`model` takes a selector), `{"op": "remove", "path"}`, `{"op": "add", "kind":
"tool" | "validator" | "mcp-server" | "skill" | "guardrail" | "memory", ...}`
with the keyword arguments of the matching `hiveloom.construct` function
(never `code`), and `{"op": "write", "path", "content"}` for a text file under
`schemas/` or `skills/`.

**Evolving.** The application sends the bundle and the journals it kept for
the current version. The worker ingests them into a temporary Hive, analyses
the current version's failures and drafts one gated proposal (`trigger:
night`); with `reflect`, it also drafts lessons from each failed run (`trigger:
reflect`). The application stores them; a person applies one by sending it
back to `/proposals/apply`. A refused draft is said in `note` and does not
stop the lessons.

The journal a run returns is the one `logging.redact` already scrubbed: declare
the redaction the application's data needs before it stores journals.
