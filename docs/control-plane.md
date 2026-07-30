# The hiveloom control plane

> **Non-production by design.** No TLS — tokens are cleartext on the wire.
> No replay/nonce cache — a captured token is replayable until it expires
> (hence the short 900-second default TTL). No revocation propagation beyond
> one local file. One harness per process. Binds to `127.0.0.1` by default;
> `hiveloom serve` warns loudly on stderr if you point it anywhere else. Put
> a reverse proxy or an SSH tunnel in front if you need this reachable from
> off-box, and never expose it directly to the open internet.

`hiveloom serve <harness-dir>` exposes a deployed harness's CLI surface over
HTTP, bearer-authenticated with the ed25519 keys described below — a
**selected operational subset**, not everything `hiveloom` can do. Some
verbs are deliberately absent (`package`, `generate`, `trust`) and some
spec roots can never be reached remotely at all regardless of scope — see
below. MCP server administration in particular is local-only: `hiveloom
set mcp_servers ...` and friends are a construct-API/CLI operation, never
an HTTP one.

## Identity: ed25519 keys + bearer tokens

Built at the user's request for "quick non-production asymmetric bearer
tokens." Deliberately minimal — read the limitations at the bottom before
relying on it for anything that matters.

### Custody model

- A member runs `hiveloom keys generate <name>` **on their own machine**.
  The private key (`<name>.pem`, mode 0600) never leaves that machine.
- The member runs `hiveloom keys sign --key <name>.pem` whenever they need a
  bearer token, and sends the token (not the key) to whoever needs it.
- The operator of the deployed harness runs
  `hiveloom keys authorize <name> <public-key> --harness <dir>` on the deploy
  box, using the public key the member shared — never the private key.
- Revocation (`hiveloom keys revoke <key-id> --harness <dir>`) and listing
  (`hiveloom keys list --harness <dir>`) also run on the deploy box, against
  that harness's `<dir>/.hiveloom/authorized_keys.json` store.

### `hiveloom keys` verbs

| Command | Runs on | Effect |
| --- | --- | --- |
| `keys generate <name> [--out-dir ~/.hiveloom/keys]` | member's machine | Writes `<name>.pem` (0600, refuses to overwrite); prints the public key and its key_id. |
| `keys sign --key <path> [--subject] [--scope] [--ttl]` | member's machine | Mints a bearer token (default TTL 900s / 15 min). |
| `keys authorize <name> <public-key> --harness <dir> [--scope ...]` | deploy box | Authorizes a public key. Idempotent on key_id: re-authorizing un-revokes and updates scopes. |
| `keys revoke <key-id> --harness <dir>` | deploy box | Marks a key revoked. The row is kept, not deleted (audit trail). |
| `keys list --harness <dir>` | deploy box | Lists authorized keys (including revoked ones). |

All five support `--json`. The store path defaults to
`<harness_dir>/.hiveloom/authorized_keys.json` and can be overridden with
`--authorized-keys PATH` or `$HIVELOOM_AUTHORIZED_KEYS`.

**A public key is base64url, whose alphabet includes `-`.** About 1 in 64
freshly generated keys start with one, which a shell/CLI argument parser
can misread as an option flag (`No such option: -a`) rather than the
`<public-key>` value — this is a standard Click/argparse ambiguity, not
specific to `hiveloom`. If `keys authorize` rejects an otherwise-correct
invocation this way, put `--` before the positional arguments so the
parser stops looking for options: `hiveloom keys authorize --harness
<dir> --scope run -- <name> <public-key>`.

Scopes are coarse by design — `run`, `read`, `mutate`, `evolve`, `*` — with no
per-route ACLs. A token cannot grant more than its authorizing key holds:
both the key's scopes and the token's own `scope` claim must cover whatever
is required.

## Serving a harness

```
hiveloom serve <dir> [--host 127.0.0.1] [--port 8420]
                     [--max-concurrent-runs 1] [--max-queued-runs 4]
                     [--authorized-keys PATH] [--approve]
```

Trust is checked exactly once, before the socket binds — never per request.
A non-interactive invocation (no TTY on stdin, e.g. systemd/CI/Docker)
refuses to start against an untrusted harness folder rather than hang
waiting for a prompt that will never come; pass `--approve` or trust the
directory beforehand with `hiveloom trust <dir>`.

### Endpoints

Every endpoint maps 1:1 onto an existing library function — the CLI and the
HTTP layer share one implementation of each behavior, never two. Bodies are
JSON; responses follow the same `{"ok": ...}` shape the CLI's `--json` output
uses.

| Method/path | Scope | Wraps |
| --- | --- | --- |
| `GET /health` | none | Harness name, spec version hash, evolved counter. |
| `POST /run` | `run` | `runner.run_harness`. `?stream=true` → SSE of trace events, ending in a `run_result` frame. |
| `GET /stats` | `read` | Hive summary + recent failures. |
| `GET /trace/{run_id}` | `read` | Hive lookup + trace read, bound to the served harness; 404 if unknown *or if the run belongs to a different harness* (the Hive is global — see below). |
| `POST /validate` | `read` | Full spec + code-hook validation. |
| `POST /set` | `mutate` | Set one dotted field to an already-typed JSON value (`construct.set_value`) — refused for any ALWAYS_FROZEN root; see below. |
| `POST /add/{tool,validator,guardrail,hook,skill}` | `mutate` | The matching `construct.add_*` function. `guardrail` and `hook` are always refused — both map onto ALWAYS_FROZEN roots. |
| `POST /remove` | `mutate` | `construct.remove_item` — refused for any ALWAYS_FROZEN root, whether named by dotted path or by an entry's builtin/code-ref name. |
| `POST /evolve/propose` | `evolve` | Drafts and queues a proposal (`trigger="http"`); never applies. |
| `GET /proposals` | `read` | List queued proposals for this harness (optional `?status=`). |
| `GET /proposals/{id}` | `read` | Show one proposal, bound to the served harness; 404 if unknown or if it belongs to a different harness. |
| `POST /proposals/{id}/apply` | `evolve` | Apply a queued proposal (bound to the served harness). Body: `{"approve_code": [...], "apply_yaml": bool}` — the HTTP substitute for interactive y/n; anything not listed in `approve_code` stays pending. |
| `POST /proposals/{id}/reject` | `evolve` | Reject a queued proposal, bound to the served harness. Body: `{"reason": "..."}`. |

`POST /set`'s `value` is an already-typed JSON value (a number stays a
number), not a YAML-scalar string like the CLI's positional argument — JSON
already distinguishes types, so there's no ambiguity to resolve. `POST /run`
never supports the CLI's `--file`-style server-side path reads; see the
input-handling section below.

### Frozen roots: what `mutate` scope can never reach

`construct.set_value`/`add_*`/`remove_item` are the sanctioned way to edit a
spec **locally** and deliberately ignore `ALWAYS_FROZEN` — that's the
evolver's boundary, not construct's. Reachable over HTTP, the same freedom
is a remote-configuration hole: setting `model` could repoint the executor
at an attacker-controlled endpoint (exfiltrating every prompt and tool
result), setting `logging.redact` could strip redaction so secrets land in
traces in cleartext, setting `guardrails` could remove the cost cap
entirely. So `/set`, every `/add/{kind}`, and `/remove` refuse any of
`ALWAYS_FROZEN`'s roots — `guardrails`, `model`, `logging.redact`,
`extensions`, `hooks`, `mcp_servers`, and `evolution.auto_propose` — with
**403**, not 400: this is "your scope does not permit that," not "your request
was malformed." The local CLI is completely unaffected; this check lives
entirely in the HTTP layer
(`serve/app.py`), derived from `ALWAYS_FROZEN` itself rather than a
hand-maintained parallel list, so it never drifts from what the evolver
already refuses to touch.

### Proposals and traces are bound to the served harness

The Hive (`~/.hiveloom/hive.db`) is global — every harness on the box
shares it. `GET /trace/{run_id}` and the `/proposals/{id}...` endpoints
look up by id, so without an explicit check a caller authorized for one
harness could read another harness's run traces, or read/reject/apply
another harness's proposals, just by guessing or enumerating an id. Every
such lookup is bound to the harness this process was started against;
anything belonging to a different harness comes back as a plain 404, not a
403 — the response never confirms that an id exists elsewhere.

### Status-code mapping

| Condition | HTTP status |
| --- | --- |
| A run completes — `success`, `verify_failed`, `guardrail_halt`, `max_turns`, or `error` are all valid *results*, not failures | 200 |
| Bad input (`SpecError`, a malformed field) | 400 |
| Missing/malformed/expired/revoked bearer token | 401 |
| Valid token, wrong scope, or a `/set`/`/add`/`/remove` targeting an ALWAYS_FROZEN root | 403 |
| Unknown id (run, proposal), or one that belongs to a different harness | 404 |
| `/run` at `max-concurrent-runs + max-queued-runs` capacity | 503 + `Retry-After` |
| Anything unhandled | 500 |

### Concurrency

`/run` is bounded by a small worker pool (`hiveloom.serve.runslots.RunSlots`):
`--max-concurrent-runs` (default **1**) workers, plus up to
`--max-queued-runs` (default **4**) more waiting before the endpoint starts
rejecting with 503. A harness runs one call at a time by default because a
run invokes a model and can be expensive; the queue absorbs a short burst
without ever growing unbounded. Every other endpoint (construct mutations,
evolve/propose, proposals, stats, trace) is cheap file/SQLite I/O dispatched
off the event loop without competing for run capacity.

A single `threading.Lock` (the "spec lock") serializes every mutating
endpoint's read-modify-write cycle on `harness.yaml`, closing a race between
concurrent `/set`/`/add`/`/remove`/`/proposals/{id}/apply` calls. It is
**not** held for a run's duration: an in-flight run already loaded its own
spec snapshot before starting, so a concurrent mutation only ever affects
later runs — the same semantics as redeploying a harness folder underneath
a running process.

### `POST /run` input handling (security)

The CLI's `_resolve_input` treats any existing file path as "read this
file" — fine for a trusted operator's own terminal, but arbitrary file read
on the server host if a network caller controlled that string. So over
HTTP:

- `input` is **always** literal text. A value that happens to name a real
  file on the server is sent to the model as that literal string, never
  read.
- An optional, separate `input_file` field is resolved relative to the
  harness directory and rejected if it would escape it — **and** rejected if
  it points at anything `package.py` already treats as "never leaves the
  harness": `.hiveloom/` (the trust store, construction log, and — for a
  served harness — its own `authorized_keys.json` and every prior run's
  trace), `.env*` (a deployed harness may hold live provider credentials such
  as `ANTHROPIC_API_KEY` there), or the configured `logging.trace_dir` even
  when it's been moved outside `.hiveloom/`. Staying inside the harness
  directory is necessary but not sufficient; both checks share one
  definition (`hiveloom.package.is_sensitive_path`, matched case-insensitively
  since a caller's casing isn't corrected to the on-disk name on a
  case-insensitive filesystem) so packaging and serving can never disagree
  about what counts as sensitive.
- This is the SAME protection the harness's own `file_read`/`file_write`
  tools get, and the evolver's code-change containment: all four callers go
  through `_safe_path`, which now enforces the identical sensitivity check —
  `.hiveloom/`, `.env*`, and the configured trace directory (default or
  reconfigured) — for every one of them, not just `input_file`. A harness
  with `file_read` configured cannot read its own auth store, `.env`, or a
  reconfigured trace directory's contents either: `run` scope over HTTP
  never grants more filesystem reach than the model already had running
  locally.

## Limitations (loud, on purpose)

- **No TLS.** Tokens travel in cleartext `Authorization` headers on the
  wire. Put this behind a TLS-terminating proxy if the network isn't
  already trusted.
- **No replay/nonce cache.** A captured token is replayable by anyone who
  captures it, until it expires — hence the short 900-second (15 minute)
  default TTL.
- **No revocation propagation *beyond this one store*.** Within this store,
  revocation IS immediate: `verify_bearer` reads `authorized_keys.json`
  fresh on every request (no caching), so a revoked key's tokens are
  rejected starting with the very next request to this harness — no
  waiting for a TTL to expire. What does NOT propagate: if the same public
  key was separately authorized against a different harness (a different
  `authorized_keys.json`), revoking it here leaves that other copy
  untouched — there is no central registry linking them.
- **One harness per process.** `hiveloom serve` serves exactly the directory
  it was started against. Running several harnesses means several
  processes (and, if exposed beyond loopback, several ports).
- **Localhost by default, no built-in TLS.** Binding elsewhere prints a loud
  stderr warning; there is no in-process TLS termination, rate limiting,
  mTLS, or HA — put a reverse proxy in front for any of that.
- **Non-production by design.** This trades away the hardening a real
  identity system and API gateway would have (mTLS, revocation lists,
  replay protection, key rotation ceremonies, rate limiting, audit logging
  beyond the kept-row trail) in exchange for something a single operator can
  stand up in minutes. Do not point it at anything where a captured token,
  an unrevoked key over its TTL window, or an exposed port is an
  acceptable-but-forgotten risk.
