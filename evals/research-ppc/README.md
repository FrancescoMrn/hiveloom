# Saved-page PPC pilot

This fixture exercises extraction of a title, complete quotation and source URL
as JSON. Twelve **synthetic** saved HTML pages include navigation and related
content distractors. Inputs and expected records are versioned in `pages.json`.
There is no web fetch, so source drift cannot affect an experiment.

Build with the validated construction API (the script never writes harness YAML
directly):

```sh
uv run python evals/research-ppc/build.py /tmp/ppc-articles \
  --executor claude/claude-haiku-4-5 --director claude/claude-haiku-4-5 --budget 1
uv run hiveloom validate /tmp/ppc-articles --json
uv run hiveloom eval validate /tmp/ppc-articles/eval.yaml --json
```

The builder emits three charters with the same executor, evaluator, holdout,
guards and total budget. Choose model selectors from the installed catalog.
The following commands spend model budget:

```sh
uv run hiveloom research init /tmp/ppc-articles --name current \
  --charter /tmp/ppc-articles/autonomous.yaml --json
uv run hiveloom research init /tmp/ppc-articles --name selection \
  --charter /tmp/ppc-articles/ppc_select.yaml --json
uv run hiveloom research init /tmp/ppc-articles --name composition \
  --charter /tmp/ppc-articles/ppc.yaml --json
uv run hiveloom research run /tmp/ppc-articles --name current --json
uv run hiveloom research run /tmp/ppc-articles --name selection --json
uv run hiveloom research run /tmp/ppc-articles --name composition --json
```

Check CLI help for the installed release before running. Each program measures
the unchanged harness as `c0`, the fourth comparison arm. Do not apply any
promotion between arms. The synthetic provider test in `test_research_ppc.py`
constructs format and quotation errors and verifies that a composed candidate
is measured and can reach the pending review queue. Its success demonstrates
the plumbing, not model improvement.

## Controlled comparison protocol

For a real comparison, replace this toy set with independently annotated,
versioned snapshots covering page structures and failure classes. Keep a fresh
confirmation set. Freeze the executor, validator, tool set and repetitions
before starting all arms; retain the same eval identity so split membership
matches. Never tune prompts after inspecting confirmation results. A follow-up
search requires a fresh held-out set.

Compare baseline, autonomous search, A/B selection, and A/B composition. Give
the three search arms the same total dollar ceiling and wall-clock allowance;
report actual spend as well. Director calls, composition, probes and final
confirmation count towards that ceiling. A skipped or unaffordable composition
must remain in the report. Repeat whole programs with fresh randomness for a
real-model comparison; do not select the best seed.

Report working and confirmation results separately: exact extraction accuracy,
format failures, incomplete quotes, wrong sources, false successes, total spend,
cost per correct result, and latency. Cost per correct result is total cost
divided by the number of correct outputs; report undefined when none are
correct. Include all candidate verdicts, especially compositions that lose.
The eval stores separate content metrics; research selection uses verifier
success, so the verifier must enforce semantic correctness too.

The pilot validator and scorer share deterministic checks. Its false-success
metric detects runtime/check disagreement, not annotation mistakes; a realistic
benchmark needs independent labels and audit. Twelve toy pages cannot establish
equivalence or justify automatic adoption. No real-model benchmark results are
claimed by this fixture.
