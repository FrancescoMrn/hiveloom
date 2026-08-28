/**
 * The trajectory projection: journal events -> spans, turns, and totals.
 *
 * Pure and synchronous on purpose. The JSONL journal stays the source of
 * truth; this only pairs events that the runtime already emitted in pairs
 * (`model_call`/`model_response`, `tool_call`/`tool_result` keyed by the
 * provider's call id) so the UI can show a duration instead of a gap between
 * two rows.
 *
 * What it must never do is invent an end: an unpaired opening event keeps
 * `endMs === null`, and callers render that as open rather than as zero.
 */
import type { TraceEvent } from './types'

export type EventCategory = 'run' | 'model' | 'tool' | 'context' | 'verify' | 'safety' | 'control'

export type Lane = 'model' | 'tool' | 'verify'

export interface Usage {
  input_tokens: number
  output_tokens: number
  cache_read_tokens: number
  cache_write_tokens: number
}

export interface Span {
  /** Stable within one run: the opening event's seq. */
  id: number
  lane: Lane
  label: string
  detail: string
  startSeq: number
  endSeq: number | null
  startMs: number
  endMs: number | null
  /** null while the pair is open — the journal never recorded a close. */
  durationMs: number | null
  turn: number | null
  failed: boolean
  /** tool_update / tool_retry / tool_truncated seqs carrying the same call id. */
  updateSeqs: number[]
  usage?: Usage
  costUsd?: number
  /** Sub-row inside its lane, so parallel calls do not draw on top of each other. */
  row: number
}

export interface Turn {
  turn: number
  label: string
  firstSeq: number
}

export interface Totals {
  turns: number
  modelCalls: number
  toolCalls: number
  /** Union of the spans' wall-clock intervals — parallel calls counted once. */
  modelMs: number
  toolMs: number
  wallMs: number
  usage: Usage
  costUsd: number
}

export interface Trajectory {
  spans: Span[]
  /** Every seq that belongs to a span, opening and closing alike. */
  spanBySeq: Map<number, Span>
  turnBySeq: Map<number, number>
  turns: Turn[]
  /** Closing halves of a pair — foldable into their opening row. */
  pairedSeqs: Set<number>
  totals: Totals
  startMs: number
  endMs: number
  laneRows: Record<Lane, number>
}

const EMPTY_USAGE: Usage = {
  input_tokens: 0,
  output_tokens: 0,
  cache_read_tokens: 0,
  cache_write_tokens: 0,
}

export function categoryOf(type: string): EventCategory {
  if (type.startsWith('tool_')) return 'tool'
  if (type.startsWith('model_')) return 'model'
  if (type.startsWith('context_') || type === 'user_steer') return 'context'
  if (type === 'verification_result') return 'verify'
  if (type.startsWith('guardrail_') || type.startsWith('hook_')) return 'safety'
  if (type.startsWith('run_')) return 'run'
  return 'control'
}

export function projectTrajectory(events: TraceEvent[]): Trajectory {
  const spans: Span[] = []
  const spanBySeq = new Map<number, Span>()
  const turnBySeq = new Map<number, number>()
  const turns: Turn[] = []
  const pairedSeqs = new Set<number>()

  const openModel = new Map<string, Span>()
  const openTool = new Map<string, Span>()
  const seenTurns = new Set<number>()

  let currentTurn: number | null = null

  for (const event of events) {
    const at = msOf(event)
    const payload = event.payload ?? {}
    const turn = numberOrNull(payload.turn)

    if (turn !== null) currentTurn = turn
    if (currentTurn !== null) {
      turnBySeq.set(event.seq, currentTurn)
      if (!seenTurns.has(currentTurn)) {
        seenTurns.add(currentTurn)
        turns.push({ turn: currentTurn, label: `Turn ${currentTurn}`, firstSeq: event.seq })
      }
    }

    switch (event.type) {
      case 'model_call': {
        const span: Span = {
          id: event.seq,
          lane: 'model',
          label: `Model · turn ${turn ?? '?'}`,
          detail: `${String(payload.phase ?? 'act')} phase · ${String(payload.num_messages ?? '?')} messages`,
          startSeq: event.seq,
          endSeq: null,
          startMs: at,
          endMs: null,
          durationMs: null,
          turn: currentTurn,
          failed: false,
          updateSeqs: [],
          row: 0,
        }
        spans.push(span)
        spanBySeq.set(event.seq, span)
        openModel.set(modelKey(payload), span)
        break
      }
      case 'model_response': {
        const span = openModel.get(modelKey(payload)) ?? lastOpen(spans, 'model')
        if (span) {
          close(span, event, at)
          pairedSeqs.add(event.seq)
          spanBySeq.set(event.seq, span)
          openModel.delete(modelKey(payload))
          span.usage = usageOf(payload.usage)
          span.costUsd = numberOrNull(payload.cost_usd) ?? undefined
        }
        break
      }
      case 'tool_call': {
        const span: Span = {
          id: event.seq,
          lane: 'tool',
          label: String(payload.name ?? 'tool'),
          detail: preview(payload.input, 90),
          startSeq: event.seq,
          endSeq: null,
          startMs: at,
          endMs: null,
          durationMs: null,
          turn: currentTurn,
          failed: false,
          updateSeqs: [],
          row: 0,
        }
        spans.push(span)
        spanBySeq.set(event.seq, span)
        const key = String(payload.id ?? `${payload.name}:${event.seq}`)
        openTool.set(key, span)
        break
      }
      case 'tool_result': {
        const key = String(payload.id ?? '')
        const span = openTool.get(key) ?? lastOpen(spans, 'tool')
        if (span) {
          close(span, event, at)
          pairedSeqs.add(event.seq)
          spanBySeq.set(event.seq, span)
          span.failed = isTrue(payload.is_error)
          openTool.delete(key)
        }
        break
      }
      case 'tool_update':
      case 'tool_retry':
      case 'tool_truncated': {
        const key = String(payload.id ?? '')
        const span = openTool.get(key) ?? spanOfToolName(spans, payload.name)
        if (span) {
          span.updateSeqs.push(event.seq)
          spanBySeq.set(event.seq, span)
        }
        break
      }
      case 'verification_result': {
        const span: Span = {
          id: event.seq,
          lane: 'verify',
          label: String(payload.verifier ?? 'verifier'),
          detail: String(payload.feedback ?? ''),
          startSeq: event.seq,
          endSeq: event.seq,
          startMs: at,
          endMs: at,
          durationMs: 0,
          turn: currentTurn,
          failed: !isTrue(payload.passed),
          updateSeqs: [],
          row: 0,
        }
        spans.push(span)
        spanBySeq.set(event.seq, span)
        break
      }
      default:
        break
    }
  }

  const stamps = events.map(msOf).filter(Number.isFinite)
  const startMs = stamps.length ? Math.min(...stamps) : 0
  const endMs = stamps.length ? Math.max(...stamps) : 0

  const laneRows = { model: 1, tool: 1, verify: 1 } as Record<Lane, number>
  for (const lane of ['model', 'tool', 'verify'] as Lane[]) {
    laneRows[lane] = packLane(spans.filter((span) => span.lane === lane), endMs)
  }

  return {
    spans,
    spanBySeq,
    turnBySeq,
    turns,
    pairedSeqs,
    totals: totalsOf(spans, events, startMs, endMs),
    startMs,
    endMs,
    laneRows,
  }
}

/** Greedy interval packing: a span only drops to a new row if it overlaps one already there. */
function packLane(spans: Span[], fallbackEnd: number): number {
  const rowEnds: number[] = []
  for (const span of spans.slice().sort((a, b) => a.startMs - b.startMs)) {
    const end = span.endMs ?? fallbackEnd
    let row = rowEnds.findIndex((rowEnd) => span.startMs >= rowEnd)
    if (row === -1) {
      row = rowEnds.length
      rowEnds.push(end)
    } else {
      rowEnds[row] = end
    }
    span.row = row
  }
  return Math.max(1, rowEnds.length)
}

function totalsOf(spans: Span[], events: TraceEvent[], startMs: number, endMs: number): Totals {
  const usage: Usage = { ...EMPTY_USAGE }
  let costUsd = 0
  for (const span of spans) {
    if (span.lane !== 'model') continue
    if (span.usage) {
      usage.input_tokens += span.usage.input_tokens
      usage.output_tokens += span.usage.output_tokens
      usage.cache_read_tokens += span.usage.cache_read_tokens
      usage.cache_write_tokens += span.usage.cache_write_tokens
    }
    costUsd += span.costUsd ?? 0
  }
  const turns = new Set<number>()
  for (const event of events) {
    const turn = numberOrNull(event.payload?.turn)
    if (turn !== null) turns.add(turn)
  }
  return {
    turns: turns.size,
    modelCalls: spans.filter((span) => span.lane === 'model').length,
    toolCalls: spans.filter((span) => span.lane === 'tool').length,
    modelMs: unionMs(spans.filter((span) => span.lane === 'model')),
    toolMs: unionMs(spans.filter((span) => span.lane === 'tool')),
    wallMs: Math.max(0, endMs - startMs),
    usage,
    costUsd,
  }
}

/** Wall-clock occupancy, not the sum: two tools running at once cost one interval. */
function unionMs(spans: Span[]): number {
  const closed = spans
    .filter((span): span is Span & { endMs: number } => span.endMs !== null)
    .map((span) => [span.startMs, span.endMs] as const)
    .sort((a, b) => a[0] - b[0])
  let total = 0
  let cursor = -Infinity
  for (const [start, end] of closed) {
    const from = Math.max(start, cursor)
    if (end > from) total += end - from
    cursor = Math.max(cursor, end)
  }
  return total
}

function close(span: Span, event: TraceEvent, at: number) {
  span.endSeq = event.seq
  span.endMs = at
  span.durationMs = Number.isFinite(at) && Number.isFinite(span.startMs)
    ? Math.max(0, at - span.startMs)
    : null
}

function modelKey(payload: Record<string, unknown>): string {
  return `${String(payload.turn ?? '?')}:${String(payload.phase ?? 'act')}`
}

function lastOpen(spans: Span[], lane: Lane): Span | undefined {
  for (let index = spans.length - 1; index >= 0; index -= 1) {
    if (spans[index].lane === lane && spans[index].endSeq === null) return spans[index]
  }
  return undefined
}

function spanOfToolName(spans: Span[], name: unknown): Span | undefined {
  for (let index = spans.length - 1; index >= 0; index -= 1) {
    if (spans[index].lane === 'tool' && spans[index].label === String(name)) return spans[index]
  }
  return undefined
}

function usageOf(value: unknown): Usage {
  const record = value && typeof value === 'object' ? (value as Record<string, unknown>) : {}
  return {
    input_tokens: numberOrNull(record.input_tokens) ?? 0,
    output_tokens: numberOrNull(record.output_tokens) ?? 0,
    cache_read_tokens: numberOrNull(record.cache_read_tokens) ?? 0,
    cache_write_tokens: numberOrNull(record.cache_write_tokens) ?? 0,
  }
}

function msOf(event: TraceEvent): number {
  return Date.parse(event.timestamp)
}

function numberOrNull(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value === 'string' && value.trim() !== '' && Number.isFinite(Number(value))) {
    return Number(value)
  }
  return null
}

function isTrue(value: unknown): boolean {
  return value === true || value === 'True' || value === 'true' || value === 1
}

/* ------------------------------------------------------------------ format */

export function formatMs(milliseconds: number | null): string {
  if (milliseconds === null || !Number.isFinite(milliseconds)) return '—'
  if (milliseconds < 1000) return `${Math.round(milliseconds)}ms`
  if (milliseconds < 10_000) return `${(milliseconds / 1000).toFixed(2)}s`
  return `${(milliseconds / 1000).toFixed(1)}s`
}

export function formatTokens(count: number): string {
  if (count < 1000) return String(count)
  return `${(count / 1000).toFixed(count < 10_000 ? 1 : 0)}K`
}

export function preview(value: unknown, limit = 140): string {
  let text: string
  if (typeof value === 'string') text = value
  else {
    try {
      text = JSON.stringify(value) ?? String(value)
    } catch {
      text = String(value)
    }
  }
  text = text.replace(/\s+/g, ' ').trim()
  return text.length > limit ? `${text.slice(0, limit - 1)}…` : text
}
