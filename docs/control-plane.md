# The hiveloom control plane

*Stub — this task (WS4a) covers only identity and auth. The HTTP server,
its endpoints, `hiveloom serve`, and run-slot concurrency arrive in the next
task, which will expand this document.*

## Identity: ed25519 keys + bearer tokens

**This is explicitly a non-production auth layer**, built at the user's
request for "quick non-production asymmetric bearer tokens." It is
deliberately minimal — read the limitations below before relying on it for
anything that matters.

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

### Limitations (loud, on purpose)

- **No TLS.** Tokens travel in cleartext `Authorization` headers on the wire.
  Put this behind a TLS-terminating proxy if the network isn't already
  trusted.
- **No replay/nonce cache.** A captured token is replayable by anyone who
  captures it, until it expires — hence the short 900-second (15 minute)
  default TTL.
- **No revocation propagation.** Revoking a key only updates the one
  `authorized_keys.json` file it's checked against. A token already issued
  under that key stays valid (subject to the TTL above) until it expires;
  there is no live push of revocation state anywhere else.
- **Non-production by design.** This trades away the hardening a real
  identity system would have (mTLS, revocation lists, replay protection,
  key rotation ceremonies, audit logging beyond the kept-row trail) in
  exchange for something a single operator can stand up in minutes. Do not
  point it at anything where a captured token or an unrevoked key over its
  TTL window is an acceptable-but-forgotten risk.
