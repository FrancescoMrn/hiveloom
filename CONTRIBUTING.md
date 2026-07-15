# Contributing to hiveloom

Thanks for contributing. hiveloom treats a harness as a deployable artifact, so
changes to the runtime, the spec contract, examples, and documentation should
stay aligned.

## Development setup

Requires Python 3.11 or newer. [uv](https://docs.astral.sh/uv/) is the
recommended workflow:

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
uv build
```

Use the CLI construction commands when changing a harness. They validate and
roll back invalid mutations:

```bash
uv run hiveloom validate harnesses/example-summarizer
uv run hiveloom run harnesses/example-summarizer --input notes.txt --dry-run
```

Live runs need `ANTHROPIC_API_KEY`; tests use fake providers and must not need
credentials or network access.

## Pull requests

- Keep a change focused and add or update tests for behavior changes.
- Update the relevant reference documentation and example harnesses when the
  public spec, CLI, extension API, or deployment artifact changes.
- Run the commands above before opening a pull request.
- Do not commit `.env`, `.token.*`, traces, generated artifacts, or credentials.

## Versioning and releases

The project uses semantic versioning. Update the project version in
`pyproject.toml`; `src/hiveloom/__init__.py` exposes the matching runtime
version. Build release artifacts with `uv build` and validate a harness package
before publishing.

Security-sensitive issues should follow [SECURITY.md](SECURITY.md), not be
reported in a public issue.
