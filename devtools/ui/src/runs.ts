import type { RunRow } from './types'

// Small on purpose: 32×32 gives ~1000 combinations, and the short hash the
// auto-alias carries is what actually disambiguates. The words only make the
// hash pronounceable.
const ALIAS_ADJECTIVES = [
  'amber', 'bold', 'brisk', 'calm', 'clear', 'crisp', 'deft', 'dry',
  'eager', 'fleet', 'fond', 'glad', 'grand', 'green', 'keen', 'kind',
  'late', 'light', 'lone', 'loud', 'mild', 'neat', 'pale', 'plain',
  'proud', 'quick', 'ripe', 'sharp', 'still', 'swift', 'warm', 'wise',
]
const ALIAS_NOUNS = [
  'aspen', 'birch', 'brook', 'cedar', 'cliff', 'cloud', 'coast', 'cove',
  'crane', 'dune', 'fern', 'field', 'fjord', 'glen', 'grove', 'heath',
  'heron', 'lake', 'marsh', 'meadow', 'otter', 'pine', 'plume', 'reef',
  'ridge', 'river', 'shoal', 'slope', 'spruce', 'stone', 'vale', 'wren',
]

/**
 * The deterministic auto-generated alias for a run: two words picked from the
 * run id plus its short hash, e.g. `brisk-otter-e8a7`. Pure function of the
 * id, so every surface (and every reload) derives the same name with nothing
 * stored anywhere.
 */
export function autoAlias(runId: string): string {
  const hex = runId.replace(/^run_/, '')
  const n = parseInt(hex.slice(0, 8), 16) || 0
  const adjective = ALIAS_ADJECTIVES[n % ALIAS_ADJECTIVES.length]
  const noun = ALIAS_NOUNS[Math.floor(n / ALIAS_ADJECTIVES.length) % ALIAS_NOUNS.length]
  return `${adjective}-${noun}-${hex.slice(0, 4)}`
}

/**
 * A human label for a run row, everywhere one is listed (the rail, the Runs
 * tab, version drill-downs).
 *
 * A person's alias wins when they set one; otherwise the run gets its
 * deterministic auto-alias. The task statement deliberately does NOT name the
 * run — an input like `Hello` or a pasted document makes an unreadable list —
 * it belongs in the tooltip and the detail view, next to the full id.
 */
export function runLabel(run: Pick<RunRow, 'run_id' | 'alias'>): string {
  const alias = (run.alias ?? '').replace(/\s+/g, ' ').trim()
  return alias || autoAlias(run.run_id)
}

/**
 * The run worth comparing for a version: successful first, then cheapest, then
 * fewest turns. A version with no successful run has no best run — comparing
 * two failures says nothing that the failure signatures do not already say.
 */
export function bestRun(runs: RunRow[], version: string): RunRow | null {
  const candidates = runs.filter(
    (run) => run.harness_version_hash === version && run.status === 'success',
  )
  if (candidates.length === 0) return null
  return [...candidates].sort(
    (a, b) => (a.cost_usd ?? 0) - (b.cost_usd ?? 0) || (a.turns ?? 0) - (b.turns ?? 0),
  )[0]
}

/**
 * The minimal shape `conversationFrom` reads.
 *
 * Structural rather than imported: the live `Turn` lives with the hook that
 * owns it, and this module stays free of React so it can be tested as the pure
 * function it is.
 */
export interface ThreadTurn {
  role: 'user' | 'assistant' | 'steer'
  content: string
  versionBoundary?: unknown
}

/**
 * The thread, as a conversation the runner will accept.
 *
 * `split_conversation` requires strictly alternating roles, and it is right to:
 * the major provider APIs reject consecutive same-role messages, and finding
 * that out as an opaque provider 400 is far worse than finding it out here. But
 * a thread on screen legitimately produces runs of one role — a steer lands
 * between a user message and the reply it was steering, a turn that errored has
 * no assistant text to send, a replayed run may have carried no task of its own.
 *
 * So the on-screen shape and the wire shape are not the same thing, and this is
 * where they part: empty turns drop out, a run of same-role turns merges into
 * one message with a blank line between them, and a leading assistant message
 * is dropped because a conversation that opens with a reply is not one.
 */
export function conversationFrom(turns: ThreadTurn[]): { role: string; content: string }[] {
  const rows: { role: string; content: string }[] = []
  for (const turn of turns) {
    if (turn.versionBoundary || !turn.content.trim()) continue
    const role = turn.role === 'assistant' ? 'assistant' : 'user'
    const last = rows[rows.length - 1]
    if (last && last.role === role) last.content = `${last.content}\n\n${turn.content}`
    else rows.push({ role, content: turn.content })
  }
  while (rows.length > 0 && rows[0].role === 'assistant') rows.shift()
  return rows
}
