# Security policy

## Supported versions

Security fixes are applied to the latest 0.1.x release line while hiveloom is
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
