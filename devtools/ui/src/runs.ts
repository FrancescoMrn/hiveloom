import type { RunRow } from './types'

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
