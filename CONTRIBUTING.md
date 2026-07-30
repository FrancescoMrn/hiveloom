# Contributing to hiveloom

Thanks for contributing. hiveloom treats a harness as a deployable artifact, so
changes to the runtime, the spec contract, examples, and documentation should
stay aligned.

## Development setup

Requires Python 3.11 or newer. [uv](https://docs.astral.sh/uv/) is the
recommended workflow:

```bash
uv sync --extra dev
uv run pytest --cov --cov-report=term-missing
uv run ruff check .
uv build
```

Use the CLI construction commands when changing a harness. They validate and
roll back invalid mutations:

```bash
uv run hiveloom validate harnesses/example-summarizer
uv run hiveloom run harnesses/example-summarizer --input notes.txt --dry-run
```

The unit and integration suite uses fake providers and must not need credentials
or network access. Before release, also run the live QA that matches the
provider or transport you changed:

```bash
# Anthropic end-to-end smoke through a real example harness
ANTHROPIC_API_KEY=... HIVELOOM_TRUST=always \
  uv run hiveloom run harnesses/example-summarizer --input notes.txt --json

# OpenAI-compatible three-turn/tool-call smoke
HIVELOOM_LIVE_SMOKE=1 uv run python scripts/smoke_openai_compat.py \
  --base-url <url> --api-key-env <ENV_NAME> --model <model-id>

# Live URL drift check and full comparative benchmark
cd evals/article-extractor
uv run python dataset/check_dataset_urls.py
./scripts/run_all_arms.sh
```

Live QA is intentionally separate from CI because it needs credentials, network
access, mutable web pages, or local model servers. Record the command, model,
date, and outcome in the change handoff; never commit its credentials or logs.

## Pull requests

- Keep a change focused and add or update tests for behavior changes.
- Update the relevant reference documentation and example harnesses when the
  public spec, CLI, extension API, or deployment artifact changes.
- Run the commands above before opening a pull request.
- Do not commit `.env`, `.token.*`, traces, generated artifacts, or credentials.

## Versioning and releases

The project uses semantic versioning. Update the project version in
`pyproject.toml`; `src/hiveloom/__init__.py` exposes the matching runtime
version. Record notable changes in [CHANGELOG.md](CHANGELOG.md) under an
`[Unreleased]` heading as part of the same pull request. Build release
artifacts with `uv build` and validate a harness package before publishing.

Security-sensitive issues should follow [SECURITY.md](SECURITY.md), not be
reported in a public issue.
