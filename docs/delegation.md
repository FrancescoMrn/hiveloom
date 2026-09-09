# Delegation — a phone line for every harness

A harness is one task, confined. Sooner or later it meets a request that
another harness on the same machine does better: a summarizer handed a
ranked-retrieval question, a triage bot handed a log forensics dump. Without
delegation the run has two options, both bad — attempt it anyway, or fail.

Delegation adds a third: **look for a peer, hand the task over, and verify the
answer yourself**. Failing that, *refer* — name the harness that would have
fitted, so the user can go there directly.

```yaml
delegation:
  enabled: true
  when: [on_start, model_choice]
  min_peer_success_rate: 0.7
  min_peer_runs: 5
  max_depth: 2
  budget_share: 0.5
  exclude: [scratch-harness]
```

## The fixed model, and the dynamic help

The executor **model** is the user's decision. It is in `ALWAYS_FROZEN`:
neither evolution nor a remote caller can move a harness onto a pricier model
or a different lab. That does not change here.

Delegation is the *other* axis, and it is deliberately dynamic. Which peer
fits this task, and whether its measured odds justify the hand-off, is a
question about *evidence*, and the evidence changes with every run. So
`delegation` is **not** frozen: evolution may tune `when`, the fitness floors,
`max_depth`, `budget_share` and `exclude` on the harness's own history. What
evolution still cannot do is change who executes — only which harness runs.

## The three modes

`when` selects them; a harness may use any combination.

| Mode | When it fires | What happens |
|---|---|---|
| `on_start` | before the first model turn | One selection call; if a peer is chosen, the whole task goes to it and the parent never takes a turn of its own. |
| `on_verify_fail` | after `verify.on_fail.max_retries` are exhausted | One last hand-off before reporting `verify_failed`. Happens at most once per run. |
| `model_choice` | throughout the run | One deferred `delegate__<peer>` tool per eligible peer, plus an always-active `list_peers`. The model decides mid-run. |

**Enforcement is the point.** A live test on Haiku showed that a system-prompt
instruction to "search for a specialist first" is simply skipped; a required
runtime step fixed it. So `on_start` and `on_verify_fail` are executed by the
loop, not requested of the model. `model_choice` is the one place the model
decides — *which* peer is a judgement — while whether the hand-off is allowed,
how deep it may go, and what it may spend are not its call.

**Verification never travels with the task.** The peer runs its own validators
on its own contract; the parent then re-runs *its* validators on the answer it
adopts. A peer that failed cannot be laundered into a success by the parent's
validators passing on a partial answer: the parent adopts the child's status
in that case.

## The directory, and fitness

Peers come from this machine's harness registry (`hiveloom registry add`, see
`hiveloom registry list`). `directory: local` is the only implemented value.

Never offered: the harness itself, a folder that is not trusted on this
machine, a registered folder whose spec no longer loads, and anything named in
`exclude`. Trust is checked *before* a peer's spec is read, because loading a
spec imports its declared extensions.

Each candidate carries its Hive fitness — total runs, success rate, average
cost — keyed on harness *identity*, so a same-named harness elsewhere never
inflates the number a hand-off is decided on.

* `min_peer_runs` is a floor on *measurement*: below it a peer is unmeasured,
  and an unmeasured peer can never satisfy a positive
  `min_peer_success_rate`.
* `min_peer_success_rate` is the floor on measured odds. A peer under either
  floor is never chosen automatically — but it is still **referable**.

## Referrals

Whenever selection happens and nothing is chosen (nothing fits, everything is
below the floor, the depth is spent) — or the model calls `list_peers` — the
candidates are recorded on the result:

```json
{
  "referrals": [
    {"harness": "ranked-retrieval", "description": "...",
     "success_rate": 0.82, "total_runs": 44, "reason": "below_fitness"}
  ]
}
```

`reason` is one of `no_candidates`, `below_fitness`, `none_fit`, `unparsable`,
`depth`, `cycle`, `budget`, `listed`. This is how a run answers "I am not the
right harness for this — ask *that* one" instead of guessing.

## Selection

One constrained call, to the **parent's own configured provider and model**.
Delegation never introduces a second model: the point is to change which
harness runs the task, not which model the user chose. The call lists the
eligible peers with their descriptions and fitness and asks for exactly one
name or `none`; the reply is parsed strictly, and anything else is treated as
"no choice" (with a `unparsable` referral reason). It costs nothing at all
when nothing is eligible — the common case on a machine with one harness.

The request goes through the same egress screen as every other provider call.

## Lineage

A delegated child run is started with a lineage record, written into its
`run_started` event and indexed by the Hive:

```json
{"kind": "delegation", "parent_run_id": "run_ab12…",
 "parent_harness_id": "hl-3f2a…", "depth": 1,
 "chain": ["hl-root…", "hl-parent…"]}
```

`chain` lists harness ids root → parent. It is what makes the two refusals
possible, and both are checked **before** the peer is contacted, so a refused
hand-off costs nothing:

* **depth** — a child deeper than `max_depth` (1–5, default 2) is refused;
* **cycle** — a peer already in the chain is refused.

`runs.lineage_kind` distinguishes `delegation` from `fork`, so
`hiveloom lineage <run-id>` lists both under the parent — forks as arms of one
experiment, delegated runs as work done elsewhere — and `Hive.children(run_id,
kind=…)` queries them directly.

## Cost

The parent's `max_cost_usd` guardrail is the **user's total budget**, so a
child's spend is the parent's spend: it is added to the parent's `cost_usd` as
soon as the child finishes, and reported separately as `delegated_cost_usd`
(on `RunResult`, in `run --json`, and in the `run_finished` event) so the split
stays visible.

Before a hand-off, the child is given its own cap:

```
cost_cap_usd = budget_share × (parent's cost limit − what the parent has spent)
```

installed as an extra `max_cost_usd` guardrail on the child run only — the
peer's own spec is never modified, and an extra cap can only make a run
cheaper. A cap of zero (an exhausted parent) skips the hand-off with reason
`budget`.

**Guardrail timing.** The parent's guardrails — wall clock, turn caps — are
evaluated at *turn boundaries*, so a long child run is not interrupted
mid-flight. Cost is the exception: the child carries its own ceiling and is
charged back the moment it returns.

## Trace events

| Event | Meaning |
|---|---|
| `delegation_selected` | a peer was chosen (`mode`, `harness`, fitness) |
| `delegation_started` | the hand-off begins (`depth`, `chain`, `cost_cap_usd`) |
| `delegation_finished` | the child finished (`run_id`, `status`, `cost_usd`, `turns`) |
| `delegation_skipped` | no hand-off, with `reason` (see the referral reasons above) |

`delegation_started`/`delegation_finished` survive `logging.level: summary`:
money spent elsewhere is not detail.

## Limits, and what is deliberately not here

* **Local only.** `directory: local` reads this machine's registry. A **remote
  directory over MCP** — a switchboard that offers peers on other machines,
  with the same lineage record travelling as MCP request meta — is the
  documented follow-up. The lineage keys above are already the shared contract
  with it.
* **One hand-off per mode.** `on_start` and `on_verify_fail` each delegate at
  most once; `model_choice` is bounded by the run's turn and cost caps and by
  `max_depth`.
* **No mid-flight cancellation of a child.** Stopping a parent takes effect at
  its next turn boundary.
* **Not visible in `run --dry-run`.** Peers are discovered when the run starts,
  so a dry run shows neither `list_peers` nor the `delegate__*` tools; it stays
  free of registry and Hive lookups.
* **The child is a separate run.** It has its own tools, budget, guardrails,
  confinement, and journal; the parent adopts only its final output. Nothing
  of the parent's private state, spilled results, or conversation is handed
  over — only the task statement.
