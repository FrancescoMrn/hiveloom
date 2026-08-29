# Decide

You have the evidence; now commit to a decision. You are on the larger model
and you have no file tool — everything you need is already in the
conversation, and re-reading it would only spend tokens re-deriving what the
triage step established.

Answer with exactly one JSON object:

- `severity`: `low`, `medium`, or `high`. Reserve `high` for irreversible or
  spreading damage; a stalled queue that has lost nothing is not `high`.
- `owner`: the team that owns the failing component.
- `action`: the single next step, concrete enough to carry out.

No prose before or after the object.
