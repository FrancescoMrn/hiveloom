# The link/sync protocol

`hiveloom cloud link` pairs a local harness directory with a server, and
`hiveloom cloud sync` (or `--sync` on `hiveloom run`) keeps the two sides
converged. The protocol was built for hiveloom-cloud, but nothing in the
client is specific to it: **any server implementing the three endpoints
below can be linked.** This document is the contract a third-party server
must satisfy.

Current protocol version: **1**.

## Model

- The **server is the source of truth for the harness artifact** (the YAML
  spec plus its files). Pull overwrites local harness files wholesale.
- The **client is the source of truth for run traces**. Push uploads the
  local `*.jsonl` trace files; the server must be idempotent by `run_id`.
- `sync` = push, then pull.
- Pull never touches `.hiveloom/` (traces and the link config) or `.env*`
  paths — the server's packaged zip must exclude them for the same reason.
- Pull is versioned by an opaque `version_hash`: the client stores the hash
  of its last pull and only downloads again when the server's hash differs.

## Client-side state

`hiveloom cloud link URL TOKEN` writes `<dir>/.hiveloom/cloud.json`:

```json
{
  "base_url": "https://app.example.com",
  "token": "hl_link_…",
  "version_hash": "aaa1111"
}
```

The token is opaque to the client — the `hl_link_` prefix is a
hiveloom-cloud convention, not a requirement. It is sent verbatim on every
request as `Authorization: Bearer <token>`.

## Endpoints

All endpoints are rooted at the linked origin and bearer-authenticated. A
`401` from any endpoint means the token was revoked; the client tells the
user to re-link. Other non-2xx statuses surface as errors with up to 300
bytes of the response body as detail.

### `GET /api/link/status`

Returns JSON describing the linked harness:

```json
{
  "slug": "demo-notes",
  "name": "Demo notes",
  "version_hash": "aaa1111",
  "protocol": 1
}
```

- `slug` (required) — directory-safe identifier; used as the default
  directory name at link time.
- `version_hash` (required) — opaque string identifying the current harness
  version. Any change signals the client to pull.
- `protocol` (optional) — the protocol revision the server speaks. Absent
  means `1` (the initial revision, before the field existed). The client
  refuses to proceed on a mismatch with a message telling the user which
  side to upgrade.
- `name` (optional) — human-readable display name.

Extra fields are ignored, so servers may add their own.

### `GET /api/link/pull`

Returns the packaged harness as a zip (`application/zip` body). The archive
must place every file under a single top-level folder (conventionally the
slug); the client strips that folder and extracts the rest into the linked
directory. The zip must not contain `.hiveloom/` or `.env*` entries, and
the client refuses entries whose path would escape the harness directory.

### `POST /api/link/traces`

The client uploads every `*.jsonl` file in the spec's trace directory
(default `./.hiveloom/traces`):

```json
{
  "files": [
    {"name": "run_a.jsonl", "content": "{\"run_id\": \"run_a\"}\n"}
  ]
}
```

The client re-uploads all trace files on every push, so ingestion **must be
idempotent by `run_id`**. Respond with JSON; `run_count` (the number of
runs now known server-side, optional) is echoed back to the user. The
client skips the request entirely when there are no trace files.

## Versioning rules

- The version applies to the whole contract, not per-endpoint.
- Additive changes (new optional response fields, new endpoints) do not bump
  the version; clients ignore unknown fields.
- Breaking changes (removed/renamed fields, changed payload shapes) bump
  `protocol`. A server may serve multiple revisions, but `status` advertises
  exactly one — the one the rest of the endpoints speak.
- The client's supported revision is `hiveloom.cloud.PROTOCOL_VERSION`.

## Trust model — read before linking a third-party server

Linking a server means trusting it with **code execution on your machine**:
pulled harness files can include Python extensions that hiveloom imports
when the spec loads. Treat `hiveloom cloud link` like adding a package
index or a git remote you install from — only link servers you control or
trust, and prefer read-only inspection of the pulled files before serving
a harness from an unfamiliar origin.

Other properties to be aware of:

- The link token is a capability for everything above; anyone holding it
  can read your uploaded traces and ship you harness files. Revoke and
  re-mint on any suspicion.
- Traces can contain whatever your runs contained — prompts, model output,
  tool results. Push sends them to the linked server; don't link a server
  you wouldn't show your traces to.
- The client refuses `http://` origins for non-local hosts unless you pass
  `--allow-insecure-http` at link time, because the bearer token would
  travel in cleartext. `localhost`/`127.0.0.1`/`::1` are exempt for local
  development.
- Zip extraction rejects path-traversal entries, and pull preserves
  `.hiveloom/` and `.env*` — a malicious server still cannot read local
  secrets through this protocol, but it can ship code that does once
  executed. That is the boundary that matters.
