# Triage

You are collecting evidence, not deciding. Read `incident.txt` with the file
tool and establish, from the text alone:

- what is degrading, and since when;
- what has been ruled out already;
- whether anything is irreversible (data loss, corruption) or merely stalled.

Do not propose an owner, a severity, or a remedy while you are in this
playbook — you are on the cheap model precisely because this part is reading
rather than judging. As soon as the evidence above is on the table, call
`switch_playbook` for `decide` and say what you found.
