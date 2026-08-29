# hiveloom-copilot

Help a user create, understand, test, diagnose, improve, and present Hiveloom harnesses without requiring framework-specific knowledge.

## What this is

A self-contained **hiveloom** harness. The folder is the harness; the runtime
(`pip install hiveloom`) executes it.

## Run

```bash
cp .env.example .env   # fill in ANTHROPIC_API_KEY
hiveloom run . --input "List the harnesses in this workbench"
```

The harness's code tools require the caller-owned `workbench` service injected
by `devtools/ui/server.py`; a direct run is useful for validation and dry-run
assembly, while real operations happen through the workbench API. Copilot traces
are kept separate from every target harness's traces and fitness.
