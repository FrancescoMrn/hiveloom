# The hiveloom control plane

> **Non-production by design.** No TLS — tokens are cleartext on the wire.
> No replay/nonce cache — a captured token is replayable until it expires
> (hence the short 900-second default TTL). No revocation propagation beyond
> one local file. One harness per process. Binds to `127.0.0.1` by default;
> `hiveloom serve` warns loudly on stderr if you point it anywhere else. Put
> a reverse proxy or an SSH tunnel in front if you need this reachable from
> off-box, and never expose it directly to the open internet.

`hiveloom serve <harness-dir>` exposes a deployed harness's full CLI surface
over HTTP, bearer-authenticated with the ed25519 keys described below.

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
| `GET /trace/{run_id}` | `read` | Hive lookup + trace read; 404 if unknown. |
| `POST /validate` | `read` | Full spec + code-hook validation. |
| `POST /set` | `mutate` | Set one dotted field to an already-typed JSON value (`construct.set_value`). |
| `POST /add/{tool,validator,guardrail,hook,skill}` | `mutate` | The matching `construct.add_*` function. |
| `POST /remove` | `mutate` | `construct.remove_item`. |
| `POST /evolve/propose` | `evolve` | Drafts and queues a proposal (`trigger="http"`); never applies. |
| `GET /proposals` | `read` | List queued proposals (optional `?status=`). |
| `GET /proposals/{id}` | `read` | Show one proposal. |
| `POST /proposals/{id}/apply` | `evolve` | Apply a queued proposal. Body: `{"approve_code": [...], "apply_yaml": bool}` — the HTTP substitute for interactive y/n; anything not listed in `approve_code` stays pending. |
| `POST /proposals/{id}/reject` | `evolve` | Reject a queued proposal. Body: `{"reason": "..."}`. |

`POST /set`'s `value` is an already-typed JSON value (a number stays a
number), not a YAML-scalar string like the CLI's positional argument — JSON
already distinguishes types, so there's no ambiguity to resolve. `POST /run`
never supports the CLI's `--file`-style server-side path reads; see the
input-handling section below.

### Status-code mapping

| Condition | HTTP status |
| --- | --- |
| A run completes — `success`, `verify_failed`, `guardrail_halt`, `max_turns`, or `error` are all valid *results*, not failures | 200 |
| Bad input (`SpecError`, a malformed field) | 400 |
| Missing/malformed/expired/revoked bearer token | 401 |
| Valid token, wrong scope | 403 |
| Unknown id (run, proposal) | 404 |
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
  trace), `.env*` (a deployed harness routinely holds a live
  `ANTHROPIC_API_KEY` there), or the configured `logging.trace_dir` even
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
- **No revocation propagation.** Revoking a key only updates the one
  `authorized_keys.json` file it's checked against. A token already issued
  under that key stays valid (subject to the TTL above) until it expires;
  there is no live push of revocation state anywhere else.
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
