# delegation-lab

> **Proves:** a harness hands a task to a peer that does it better — but only
> once the peer has *earned* it in measured runs — verifies the peer's answer
> with its own validators, and otherwise names the peer instead of guessing.

Offline, no API key. Two harnesses: `front-desk` (this folder) answers customer
questions from a few facts and has no ledger; `ledger-desk`
(`peers/ledger-desk/`) is a specialist that looks invoices up. Both run on
scripted providers that answer only from what they are actually shown — the
desk even answers the runtime's routing question by reading the peer
descriptions it is given.

## Capabilities

- **Delegation, `on_start` mode** — one enforced routing call before the first
  turn; the model does not get to skip it. ([delegation.md](../../docs/delegation.md))
- **Fitness floors** — `min_peer_runs: 3`, `min_peer_success_rate: 0.8`: an
  unmeasured peer is never chosen automatically, only referred.
- **Referrals** — `referrals` on the result say which harness would fit and why
  it was not used (`below_fitness`, `none_fit`, …).
- **Verification stays home** — the desk re-runs its own validator on the answer
  it adopts from the peer.
- **Lineage and cost** — the peer's run is a `delegation` child in the Hive
  (`hiveloom lineage`), its spend charged to the desk as `delegated_cost_usd`.
- **Trust and the registry** — a peer is only offered once trusted and
  registered on this machine.

## Run it

```bash
# The peer must be trusted and registered before the desk can see it.
hiveloom trust peers/ledger-desk
hiveloom registry add peers/ledger-desk

hiveloom run . --input-text "When is your office open?" --json
hiveloom run . --input-text "What is the amount of invoice INV-1003?" --json

# The peer earns a measured record.
for id in INV-1001 inv-1005 INV-1009; do
  hiveloom run peers/ledger-desk --input-text "Amount of invoice $id?" --json
done

hiveloom run . --input-text "What is the amount of invoice INV-1003?" --json
hiveloom lineage <that run_id> --json
hiveloom run . --input-text "How long does delivery take?" --json
```

`hiveloom registry add` writes to your registry under `$HIVELOOM_HOME`; run
`hiveloom registry remove peers/ledger-desk` afterwards, or set
`HIVELOOM_HOME` to a scratch directory for the walkthrough.

## What to look for

| step | result | evidence |
|---|---|---|
| office hours | the desk answers from its facts | `referrals: [ledger-desk, below_fitness]` — the peer exists but is unmeasured |
| invoice, peer unmeasured | "I can't answer that from the front desk" | the same referral: the user is told where to go, nothing is invented |
| three peer runs | 3/3 success | the peer's Hive fitness now clears both floors |
| invoice again | "Invoice INV-1003 for Cobalt BV is 4200.00 EUR." | `delegations: [ledger-desk, success]`, `delegated_cost_usd`, and `delegation_selected`/`_started`/`_finished` in the trace |
| lineage | the child run | `lineage_kind: delegation` under the desk's run |
| delivery time | the desk answers itself | `delegation_skipped` with `none_fit`: routing chose no one for a general question |

## Try this

- Set `min_peer_runs` to 1 (`hiveloom set delegation.min_peer_runs 1`) and the
  first invoice question is delegated straight away.
- Replace `on_start` with `on_verify_fail`
  (`hiveloom set delegation.when '["on_verify_fail"]'`) and require an amount
  in the desk's answer: the desk now tries itself first and hands off only
  after its own retries fail verification.
- Serve the peer to other agents over MCP: `hiveloom mcp serve
  peers/ledger-desk` exposes it as a `run_ledger-desk` tool, with the same
  lineage and depth/cycle refusals carried in the request meta.
