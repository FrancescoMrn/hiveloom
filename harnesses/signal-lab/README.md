# signal-lab

> **Proves:** a harness finds where its own failures come from, drafts a fix
> aimed at that place, and keeps a change only when its eval confirms the
> change did what it predicted — reverting the one that did not.

Offline, no API key. A clerk looks up invoices; the ledger stores ids in
uppercase and two thirds of requests write them in lowercase. Nothing in the
task or the tool says so — the harness has to find out from its own runs, and
prove it.

The models are a scripted provider in `extensions/signal_provider.py`, and
neither script carries the answer: the clerk (`qa-clerk`) uppercases an id
only when a lesson in its prompt says to, and reports the amount parsed from
the tool result it received; the evolver and reflector (`qa-evolver`) aim at
what the signal map in their prompt measured, choose from the measured attempt
history, and draft a lesson only when the run's own evidence supports one.

## Capabilities

- **`hiveloom signal`** — failing and successful runs contrasted feature by
  feature, loss classes, and how much the sample can show; free.
  ([signal-driven-evolution.md](../../docs/signal-driven-evolution.md))
- **Reflection** (`evolution.reflect`) — a failed run is read and a lesson is
  drafted into the review queue.
- **Auto-propose with trace excerpts** — after 3 failures, a proposal is
  drafted on its own (never applied), from redacted journal windows around each
  incident rather than counts alone.
- **`evolve --experiment`** — apply, run the eval, assess pair by pair, keep a
  confirmed change, revert the rest; **`hiveloom assess`** reports each change
  against its own prediction.
- **Relevance-selected memory** — each run is shown only the lessons that
  match its task; the rest stay reachable through `search_memory`.

## Run it

```bash
hiveloom validate .

# One lookup that works, one that does not — the failed run is reflected on.
hiveloom run . --input-text "Look up invoice INV-1003 and report its amount." --json
hiveloom run . --input-text "Look up invoice inv-1004 and report its amount." --json

# Two more failures, and auto-propose drafts an aimed fix.
hiveloom run . --input-text "Look up invoice inv-1001 and report its amount." --json
hiveloom run . --input-text "Look up invoice inv-1002 and report its amount." --json
hiveloom proposals list . --json

# Measure on the twelve-case eval, locate, then two measured rounds.
hiveloom eval run eval.yaml --approve --json
hiveloom signal .
hiveloom evolve . --experiment eval.yaml --yes --rounds 2 --model signal_lab/qa-evolver
hiveloom assess .

hiveloom memory list .
hiveloom run . --input-text "Look up invoice inv-1010 and report its amount." --json
```

## What to look for

| step | evidence |
|---|---|
| the lowercase lookup | `verify_failed`; a `reflect` proposal waits in the queue with a lesson citing `'inv-1004'` |
| third failure | an `auto` proposal, pending, aimed at `tool_error:lookup_invoice` — "nothing succeeded to contrast against", so it aimed at what every failure shares — with incident excerpts in its evidence |
| `signal` after the eval | `tool_error:lookup_invoice` in 100% of failures and 0% of successes (p ≈ 0.0005); every failure in the `tooling` loss class |
| round 1 | the retry idea: errors stay 8/12 → **refuted**, reverted byte for byte |
| round 2 | chosen from round 1's verdict: the uppercase rule; errors 8/12 → 0/12, success 4/12 → 12/12, 8 improved pairs and none worse (McNemar p = 0.008) → **confirmed**, kept |
| the last lookup | succeeds; `memory_selected` names two of three entries — the learned rule and the currency fact; the dispute-escalation rule matches nothing and stays out |

## Try this

- Apply the reflected lesson instead (`hiveloom proposals apply . <id> --yes`)
  and run the eval again: `assess` judges it against the success rate.
- `hiveloom set memory.selection all` and compare which lessons each run sees.

## Reset

Everything the walkthrough changes is a learned entry, and runs are indexed in
your Hive. To start over, restore the folder from version control and remove
`.hiveloom/`.
