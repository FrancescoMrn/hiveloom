/**
 * The timing overview: where a run's wall clock actually went.
 *
 * Three lanes over one shared timeline — model calls, tool calls, verifiers —
 * drawn from the paired spans in `trajectory.ts`. Clicking a bar selects the
 * event that opened it, so the overview and the ledger stay one selection.
 *
 * A span the journal never closed is drawn open-ended and hatched rather than
 * given an invented end.
 */
import type { TraceEvent } from '../types'
import type { Lane, Span, Trajectory } from '../trajectory'
import { categoryOf, formatMs, formatTokens } from '../trajectory'

const LANES: { id: Lane; label: string }[] = [
  { id: 'model', label: 'Model' },
  { id: 'tool', label: 'Tools' },
  { id: 'verify', label: 'Verify' },
]

export function Timeline({
  trajectory,
  events,
  selectedSeq,
  onSelect,
}: {
  trajectory: Trajectory
  events: TraceEvent[]
  selectedSeq: number | null
  onSelect: (seq: number) => void
}) {
  const windowMs = trajectory.endMs - trajectory.startMs
  if (!Number.isFinite(windowMs) || windowMs <= 0 || trajectory.spans.length === 0) return null

  return (
    <div className="timeline" aria-label="Run timing overview">
      {LANES.map((lane) => {
        const spans = trajectory.spans.filter((span) => span.lane === lane.id)
        const rows = trajectory.laneRows[lane.id]
        return (
          <div className="timeline-lane" key={lane.id}>
            <span className="timeline-lane-label">{lane.label}</span>
            <div className="timeline-track" style={{ height: rows * 9 + (rows - 1) * 2 }}>
              {spans.map((span) => (
                <Bar
                  key={span.id}
                  span={span}
                  windowMs={windowMs}
                  startMs={trajectory.startMs}
                  selected={selectedSeq === span.startSeq || selectedSeq === span.endSeq}
                  onSelect={onSelect}
                />
              ))}
            </div>
          </div>
        )
      })}
      <div className="timeline-lane">
        <span className="timeline-lane-label">Events</span>
        <div className="timeline-track" style={{ height: 9 }}>
          {events.map((event) => {
            const at = Date.parse(event.timestamp)
            if (!Number.isFinite(at)) return null
            return (
              <button
                key={event.seq}
                type="button"
                className={`timeline-tick cat-${categoryOf(event.type)}`}
                data-on={selectedSeq === event.seq ? '1' : '0'}
                style={{ left: `${((at - trajectory.startMs) / windowMs) * 100}%` }}
                title={`#${event.seq} ${event.type}`}
                onClick={() => onSelect(event.seq)}
              >
                <span className="sr-only">{event.type}</span>
              </button>
            )
          })}
        </div>
      </div>

      <div className="timeline-axis">
        <span>0ms</span>
        <span>{formatMs(windowMs)}</span>
      </div>
    </div>
  )
}

function Bar({
  span,
  windowMs,
  startMs,
  selected,
  onSelect,
}: {
  span: Span
  windowMs: number
  startMs: number
  selected: boolean
  onSelect: (seq: number) => void
}) {
  const left = ((span.startMs - startMs) / windowMs) * 100
  const width = span.endMs === null
    ? Math.max(0.6, 100 - left)
    : Math.max(0.6, ((span.endMs - span.startMs) / windowMs) * 100)
  return (
    <button
      type="button"
      className={`timeline-bar lane-${span.lane}`}
      data-on={selected ? '1' : '0'}
      data-open={span.endMs === null ? '1' : '0'}
      data-failed={span.failed ? '1' : '0'}
      style={{ left: `${left}%`, width: `${width}%`, top: span.row * 11 }}
      title={`${span.label} · ${span.endMs === null ? 'never closed' : formatMs(span.durationMs)}${span.detail ? ` · ${span.detail}` : ''}`}
      onClick={() => onSelect(span.startSeq)}
    >
      <span className="sr-only">{span.label}</span>
    </button>
  )
}

/** The run's accounting, from the same spans the timeline draws. */
export function RunTotals({ trajectory }: { trajectory: Trajectory }) {
  const { totals } = trajectory
  const usage = totals.usage
  const cached = usage.cache_read_tokens
  return (
    <div className="run-totals">
      <span>
        {totals.modelCalls} model {totals.modelCalls === 1 ? 'call' : 'calls'} ·{' '}
        {totals.toolCalls} tool {totals.toolCalls === 1 ? 'call' : 'calls'}
      </span>
      <span className="sep" />
      <span title="Wall-clock time inside model calls; overlapping calls counted once">
        model {formatMs(totals.modelMs)}
      </span>
      <span title="Wall-clock time inside tool calls; parallel calls counted once">
        tools {formatMs(totals.toolMs)}
      </span>
      <span title="First to last journal event">wall {formatMs(totals.wallMs)}</span>
      <span className="sep" />
      <span>
        in {formatTokens(usage.input_tokens)} · out {formatTokens(usage.output_tokens)}
        {cached > 0 ? ` · cache read ${formatTokens(cached)}` : ''}
        {usage.cache_write_tokens > 0 ? ` · cache write ${formatTokens(usage.cache_write_tokens)}` : ''}
      </span>
      {totals.costUsd > 0 && (
        <>
          <span className="sep" />
          <span className="run-totals-cost">${totals.costUsd.toFixed(4)}</span>
        </>
      )}
    </div>
  )
}
