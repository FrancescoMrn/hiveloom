# hiveloom agent skills

A series of focused [Agent Skills](https://docs.claude.com/en/docs/agents-and-tools/agent-skills)
for driving hiveloom. Each is a self-contained `SKILL.md` an agent loads on
demand; together they cover the full lifecycle:

| Skill | Use when |
|---|---|
| [`hiveloom-build`](hiveloom-build/SKILL.md) | Creating a new harness (explore → construct → validate → dry-run, or `generate`) |
| [`hiveloom-run`](hiveloom-run/SKILL.md) | Running a harness, interpreting results, reading traces and stats |
| [`hiveloom-evolve`](hiveloom-evolve/SKILL.md) | A harness keeps failing and should be improved via the gated evolve flow |
| [`hiveloom-extend`](hiveloom-extend/SKILL.md) | Adding tools/guardrails/validators/providers via extension packs |
| [`hiveloom-ship`](hiveloom-ship/SKILL.md) | Packaging, trusting foreign folders, and the deploy-and-evolve loop |

The root [`SKILL.md`](../SKILL.md) is the compact all-in-one variant — install
it alone if you want a single skill, or install this directory for
progressive disclosure per lifecycle stage.

**To install for Claude Code:** copy the skill folders into your project's
`.claude/skills/` (or `~/.claude/skills/` for all projects):

```bash
cp -r skills/hiveloom-* /path/to/project/.claude/skills/
```

**For other coding agents:** hiveloom is agent-agnostic — [AGENTS.md](../AGENTS.md)
is the entry point regardless of which agent drives it, and each skill is
plain markdown with YAML frontmatter, not a Claude-Code-specific format. An
agent without a native skills mechanism can simply be pointed at `AGENTS.md`
and the relevant `SKILL.md` files directly. An agent with its own rules or
context conventions (an `AGENTS.md` reader like Codex, a Cursor rules file,
etc.) can reference or copy these files into whatever location that
convention expects.
These files also ship inside the package, so an agent with hiveloom installed
and no checkout can read them directly: `hiveloom guide --list`, then
`hiveloom guide build` (or `run`/`evolve`/`extend`/`ship`, `all` for the
compact variant).

> Not to be confused with a *harness's own* `skills/` folder
> (`hiveloom add skill …`), which holds progressive-disclosure instructions for
> the small executor model *inside* a harness. The skills here are for the
> agent *driving* hiveloom.
