# signal-lab

Signal-driven evolution on one small task, offline, with no API key. A clerk
looks up an invoice and reports its amount. The ledger stores ids in uppercase,
and two thirds of the requests write them in lowercase. Nothing in the task or
the tool says so. The harness has to find out from its own runs, and prove it.

The walkthrough shows each 1.2.0 mechanism doing its job:

- **`hiveloom signal`** finds that `tool_error:lookup_invoice` separates
  failures from successes. It is counting, not a model call.
- **Reflection** reads a failed run and drafts a lesson into the review queue.
- **`evolve --experiment`** measures the harness on its eval and tries a
  change. It reverts the change when the eval refutes it, then tries another
  change that the measured history points to, and keeps it because the eval
  confirms it pair by pair.
- **`hiveloom assess`** reports every change against its own prediction.
- **Relevance-selected memory** shows each run only the lessons that match
  its task. The learned rule and the currency fact reach invoice lookups; the
  operator's rule about escalating disputed charges stays out.

The models are a scripted provider shipped in `extensions/signal_provider.py`.
Neither script carries the answer:

- The clerk (`qa-clerk`) uppercases an id only when a lesson in its prompt
  tells it to. It reports the amount it parsed from the tool result it
  actually received.
- The evolver (`qa-evolver`) aims at the strongest `tool_error` signal in the
  map. It chooses its change from the measured attempt history.
- The reflector (`qa-evolver`) drafts a lesson only when the run's own
  evidence (a failed lookup of a lowercase id) supports one.

## Walkthrough

```bash
hiveloom validate .

# One lookup that works, one that does not.
hiveloom run . --input-text "Look up invoice INV-1003 and report its amount." --json
hiveloom run . --input-text "Look up invoice inv-1004 and report its amount." --json

# The failed run was reflected on: a lesson waits for review, never applied.
hiveloom proposals list . --json

# Measure the harness on its twelve-case eval, then see where it fails.
hiveloom eval run eval.yaml --approve --json
hiveloom signal .
```

The signal map puts `tool_error:lookup_invoice` in 100% of failures and 0% of
successes (p about 0.0005). Every failure is in the `tooling` loss class, so
this is plumbing a harness change can reach, not a model that reasoned wrong.

```bash
# Two measured rounds with the scripted evolver.
hiveloom evolve . --experiment eval.yaml --yes --rounds 2 --model signal_lab/qa-evolver
hiveloom assess .
```

- **Round 1** tries the obvious idea, retrying the lookup on error, and
  predicts it clears the errors. All 12 cases pair up, and the error share
  stays at 8/12. The prediction is **refuted**, so the change is **reverted**
  byte for byte.
- **Round 2** reads that verdict in the attempt history and proposes the rule
  the evidence supports instead: invoice ids are uppercase. The error share
  goes from 8/12 to 0/12 and success from 4/12 to 12/12, with 8 improved pairs
  and none worse (McNemar p = 0.008). It is **confirmed** and **kept**.

```bash
hiveloom memory list .       # the learned rule, next to two operator entries
hiveloom run . --input-text "Look up invoice inv-1010 and report its amount." --json
```

The lowercase lookup now succeeds. Its trace carries a `memory_selected` event
naming two of the three stored entries: the learned rule and the currency fact,
which match the task. The dispute-escalation rule matches nothing in it and is
left out, still reachable through `search_memory`. The signal locator indexes
which runs saw which lesson.

## Reset

Everything the walkthrough changes is a learned entry, and runs are indexed in
your Hive. To start over, restore the folder from version control and remove
`.hiveloom/`.
