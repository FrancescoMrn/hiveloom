# Security policy

## Supported versions

Security fixes are applied to the latest 0.2.x release line while hiveloom is
in beta.

## Reporting a vulnerability

Please do not open a public issue for a suspected vulnerability. Report it
privately to the maintainers through the repository's security advisory or the
private contact listed by the project owner. Include a clear reproduction,
affected version, and impact assessment.

## Harness trust model

A harness can contain executable Python hooks and extensions. hiveloom trust
gates foreign harness directories before their code loads; review a harness and
use `hiveloom trust <dir>` deliberately. Keep API keys in `.env` or your
deployment secret store, never in a harness spec or source file.

## HTTP control plane (`hiveloom control-plane`)

`hiveloom control-plane` is explicitly non-production: no TLS (bearer tokens are
cleartext on the wire), no replay/nonce cache (a captured token is replayable
until it expires, hence the short 900-second default TTL), and no revocation
propagation beyond the one `authorized_keys.json` file it reads. It binds to
`127.0.0.1` by default and warns loudly on stderr if started against any other
host. Do not expose it directly to an untrusted network; put a TLS-terminating
reverse proxy or an SSH tunnel in front if it needs to be reached off-box. See
[`docs/control-plane.md`](docs/control-plane.md) for the full limitations list.
