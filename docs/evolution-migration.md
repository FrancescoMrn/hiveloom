# General-purpose evolution migration

This branch extracts reusable changes from the uncommitted `feature/arc-agi-2`
work at base `5e4e871`. The original ARC working tree is preserved. The branch
is `feature/evolution-reliability`; it does not contain the ARC dataset, scorer,
harness assets, experiment outputs, or autoresearch scripts.

## Included in this branch

| Improvement | Why it generalizes | Release behavior |
|---|---|---|
| Attempt memory | Any iterative search can repeat a rejected or reverted experiment when feedback is missing. | Recent resolved queue proposals become history; SDK drivers can supply measured histories across versions. |
| Operator findings | Passing validators do not reveal every opportunity, and old clusters may no longer describe the bottleneck. | `evolve --note` and `analyze(analyst_notes=...)` support findings even without failed runs. |
| Proposal repair | Models can violate output schemas or omit objective expectations on any task. | Up to three calls, with bounded feedback; invalid evidence fails before a call. |
| Bounded evidence | Documents, logs, retrieval results, and code can overwhelm a proposing prompt. | Redaction precedes truncation; history, findings, and failure evidence have section limits. |
| Model capabilities and provider parameters | Output limits and request controls vary by endpoint, independently of benchmark. | Positive registered output limits, validated `model.max_tokens`, bounded frozen `model.params`, and configurable transport timeouts. |
| Truncation recovery | A length-limited reply is not reliable evidence of a completed answer. | Continue within configured budgets; preserve partial output; enforce policy and verification; report `truncated` and indexed friction. |
| Provider normalization and retries | Interrupted reads, HTTP-200 error envelopes, blank content, and large reasoning payloads are transport concerns. | Transient failures retry; overflow remains classifiable; useful output survives bounded metadata/reasoning handling. |
| Hook signature validation | A callback accepting `**kwargs` cannot consume a second positional argument. | Reject that mismatch during validation instead of discovering it during a run. |

The evolution improvements apply to extraction, retrieval, triage, code tasks,
and other domains with measurable outcomes. Transport fixes improve execution
reliability. Neither category by itself establishes a task-quality gain; that
requires evaluation against an appropriate baseline.

## Corrections made during extraction

- Redact history, operator findings, and the current spec as well as failure
  records. Preserve key/path redaction by applying it before separating sections.
- Bound caller-supplied histories and wide collections, not only individual diffs.
- Preserve rejected paths and rejection reasons. Treat queue decisions as
  unmeasured, and inconclusive experiments as uncertain rather than refuted.
- Remove prompt claims that two failed prompt edits disprove all prompt changes,
  or that sampling is the only way to address a wrong answer.
- Include findings and histories in proposal deduplication, so new evidence
  cannot silently return an older pending proposal.
- Reject inconsistent metric directions before paying for proposal retries.
  Schema-repair feedback does not echo invalid values into the next prompt.
- Preserve the configured executor token budget. A provider's capability is
  not authorization to double the runtime budget after truncation.
- Report exhausted truncation as a failure unless actual verification succeeds;
  required phases cannot be bypassed. Productive policy turns reset the streak.
- Block request aliases and controls that could bypass identity, transcript,
  tool, output-budget, streaming, or response-count handling. Validate parameters
  as bounded JSON and forward them through both built-in provider families.
- Bound reasoning when it is the visible-text fallback, not just when attached
  beside a normal answer. Ignore malformed nonscalar routing metadata.
- Keep the historical strong-model output default for unknown capabilities;
  a guessed large request can be rejected by smaller endpoints.

## General-purpose work deferred

### Multiple-attempt consensus (`best_of_n`)

The concept is reusable, especially for tasks with canonical, short answers.
The implementation needs an independent release pass:

1. `context_rewound` is emitted by the new context operation but is not handled
   by journal replay. Forks/materialization can reconstruct a different context.
2. Rewinding messages does not isolate tool side effects, artifacts, or mutable
   tool state. Independence is not guaranteed for arbitrary harnesses.
3. The saved prefix is a message count; compaction can change the messages at
   those positions. Store the intended prefix or define a compatible reset.
4. Whitespace normalization can equate distinct string or code outputs. Answer
   equivalence needs an explicit contract, not a universal whitespace rule.
5. Verification sees accumulated run evidence, while the selected answer may
   come from a different attempt. Define evidence ownership, retries, and
   budget-exhaustion behavior before claiming the chosen answer is verified.

Suggested follow-up branch: `feature/consensus-policy`.

### Evaluation-driven keep/revert decisions

The ARC scripts contain reusable ideas: paired per-case comparisons, repeated
measurements, re-measuring the incumbent, recording inconclusive results,
tracking diagnostic metrics, and avoiding task selection based on unusually low
baseline scores. These belong near generic eval comparison/experiment APIs.

Do not move `scripts/decide.py` into the library unchanged. Its sign counts
support a directional sign test for general numeric scores; calling it McNemar
is specific to binary paired outcomes. It also permits a diagnostic-metric win
to keep a candidate when primary quality is inconclusive. Failure to detect a
regression is not proof that quality was preserved. A general decision contract
needs explicit metric directions and constraints, case/repetition coverage,
effect-size requirements, missing-data handling, and an approach to repeated
searches and multiple metric comparisons. Confirm selected candidates on fresh
or held-out evidence before making release-quality claims.

Suggested follow-up branch: `feature/evaluation-driven-evolution`.

### Opt-in adaptive output budgets

Automatic budget growth may be useful, but should have an operator-owned,
frozen ceiling and explicit accounting/replay semantics. It is deliberately
excluded from this release; users can raise `model.max_tokens` through the CLI.

## ARC-specific work retained in the original tree

`evals/arc-agi-2/`, its grid parser, official attempt scoring, dataset fetching,
training-pair hypothesis tools, validators, protocol arms, and benchmark test
module remain in `feature/arc-agi-2`. The experiment scripts retain their
benchmark-specific metric names and workflow until the generic experiment
contract above is designed.

## Compatibility and release notes

- No `harness.yaml` was hand-edited. Frozen evolution paths and approval gates
  remain enforced.
- Empty `model.params` is omitted from canonical serialization, preserving
  existing harness hashes and evidence cohorts.
- New `model.params` and capability fields are additive. Configurations with
  invalid capabilities or reserved request fields now fail validation.
- Callers should recognize the new `truncated` run status; CLI exit code is 4.
- Known strong-model capabilities can increase generation/evolution output
  allowances, and proposal repair can make up to three model calls. Executor
  output budgets remain unchanged.
- Automatic attempt memory covers resolved proposal-queue entries. Direct
  `evolve --yes` applications are not queue entries; measurement drivers should
  supply their own ledger. History is advice to the proposer, not a guarantee
  against duplicates or an automatic acceptance decision.
- Provider-specific parameters are retained by model overrides; switching to a
  provider with a different request contract may require an operator update.
- Changelog entries remain under `Unreleased`; the version is unchanged.
  This additive feature set is a candidate for the next minor release.

Offline validation is recorded in the change handoff. Live provider QA and a
held-out quality evaluation remain separate release checks: no paid provider
calls or ARC benchmark runs were made during this extraction.

### Validation performed

- `uv run pytest --cov --cov-report=term:skip-covered`: 1,274 passed,
  85.47% coverage (repository threshold: 85%).
- After the final default-parameter serialization compatibility fix:
  `uv run pytest tests/test_loader.py tests/test_evolution_reliability.py tests/test_truncated_turns.py`:
  54 passed.
- `uv run ruff check .` and `git diff --check` passed.
- `uv build` produced both sdist and wheel; the wheel includes the evolution
  contract and guidance, and contains no ARC assets.
- Isolated-home JSON CLI checks passed: schema emission, validation of
  `harnesses/example-summarizer`, and its `run --dry-run`.
