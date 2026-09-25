import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import type {
  Artifact,
  ForkPoint,
  HarnessDetail,
  MaterializedContext,
  RunDetail,
  RunRow,
  TraceEvent,
} from '../types'
import type { EventCategory, Span, Trajectory as TrajectoryProjection } from '../trajectory'
import { categoryOf, formatMs, formatTokens, preview, projectTrajectory } from '../trajectory'
import { runLabel } from '../runs'
import { ForkDialog } from './ForkDialog'
import { RunTotals, Timeline } from './Timeline'
import { Label, Notice, Stat, StatRow, StatusPill, when } from './common'

type Filter = 'all' | EventCategory
type InspectorTab = 'summary' | 'payload' | 'result' | 'context' | 'schema' | 'timing' | 'raw'

const FILTERS: { id: Filter; label: string }[] = [
  { id: 'all', label: 'All' },
  { id: 'model', label: 'Model' },
  { id: 'tool', label: 'Tools' },
  { id: 'context', label: 'Context' },
  { id: 'verify', label: 'Verify' },
  { id: 'safety', label: 'Safety' },
  { id: 'delegation', label: 'Delegation' },
  { id: 'control', label: 'Control' },
]

/**
 * Run history plus Hiveloom's native trajectory debugger.
 *
 * The JSONL journal remains the source of truth. The ledger is a projection of
 * its ordered events, and the Context inspector asks the backend to use the
 * same fold as `hiveloom trace --materialize` and `hiveloom fork`.
 */
export function Trajectory({
  harness,
  runId,
  runs,
  onSelectRun,
  onOpenHarness,
}: {
  harness: HarnessDetail
  /** The run the shell has selected; null shows the harness overview. */
  runId: string | null
  runs: RunRow[] | null
  onSelectRun: (runId: string | null) => void
  onOpenHarness: (harnessId: string) => Promise<void>
}) {
  const [open, setOpen] = useState<RunDetail | null>(null)
  const [selected, setSelected] = useState<TraceEvent | null>(null)
  const [filter, setFilter] = useState<Filter>('all')
  const [query, setQuery] = useState('')
  const [forking, setForking] = useState<ForkPoint | null>(null)
  const [pairCalls, setPairCalls] = useState(true)
  const [groupTurns, setGroupTurns] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    setOpen(null)
    setSelected(null)
    setError(null)
    if (!runId) return
    api
      .run(runId)
      .then((detail) => {
        if (!live) return
        setOpen(detail)
        setSelected(
          detail.events.find((event) => event.type === 'model_call') ?? detail.events[0] ?? null,
        )
        setFilter('all')
        setQuery('')
      })
      .catch((exc) => live && setError(String(exc)))
    return () => {
      live = false
    }
  }, [runId])

  const trajectory = useMemo<TrajectoryProjection | null>(
    () => (open ? projectTrajectory(open.events) : null),
    [open],
  )

  const visibleEvents = useMemo(() => {
    if (!open) return []
    const needle = query.trim().toLowerCase()
    return open.events.filter((event) => {
      if (filter !== 'all' && categoryOf(event.type) !== filter) return false
      // Folding hides the closing half of a pair; its content moves into the
      // opening row and its Result tab, and searching re-reveals nothing it
      // did not already match on.
      if (pairCalls && !needle && trajectory?.pairedSeqs.has(event.seq)) return false
      if (!needle) return true
      return `${event.type} ${JSON.stringify(event.payload)}`.toLowerCase().includes(needle)
    })
  }, [filter, open, pairCalls, query, trajectory])

  const eventBySeq = useMemo(() => {
    const map = new Map<number, TraceEvent>()
    for (const event of open?.events ?? []) map.set(event.seq, event)
    return map
  }, [open])

  if (error && !open) {
    return (
      <div className="pane">
        <Notice icon="ph-warning-octagon" tone="err" title="Could not open run" body={error} />
      </div>
    )
  }
  if (runId && !open) return <div className="empty">Loading…</div>

  if (open) {
    const run = open.run
    const integrityTone = open.integrity?.ok && open.integrity.chained
      ? 'ok'
      : open.integrity?.ok
        ? 'legacy'
        : 'bad'
    const delegation = delegationStat(open, trajectory)
    const integrityIcon = integrityTone === 'ok'
      ? 'ph-seal-check'
      : integrityTone === 'legacy'
        ? 'ph-clock-counter-clockwise'
        : 'ph-warning'
    return (
      <div className="trajectory-page">
        <div className="trajectory-head">
          <div className="trajectory-titlebar">
            <button className="v-btn v-btn-ghost v-btn-sm" onClick={() => onSelectRun(null)}>
              <i className="ph ph-arrow-left" />
              Overview
            </button>
            <StatusPill status={run.status} />
            <span className="ellipsis" style={{ fontWeight: 600 }}>{runLabel(run)}</span>
            <span className="mono run-id">{run.run_id}</span>
            <span className={`integrity ${integrityTone}`} title={open.integrity?.summary}>
              <i className={`ph ${integrityIcon}`} />
              {open.integrity
                ? open.integrity.summary
                : 'journal missing'}
            </span>
          </div>

          <div className="run-metrics">
            <Stat label="turns" value={String(run.turns)} />
            <Stat label="cost" value={`$${run.cost_usd.toFixed(4)}`} />
            <Stat label="duration" value={`${run.duration_seconds.toFixed(1)}s`} />
            <Stat label="events" value={String(open.events.length)} />
            <Stat label="version" value={run.harness_version_hash.slice(0, 12)} />
            <Stat label="model path" value={run.model_path || 'not recorded'} />
            {delegation && <Stat label="delegation" value={delegation} />}
          </div>

          {run.lineage_kind === 'delegation' && run.parent_run_id && (
            <div className="lineage-strip">
              <i className="ph ph-arrow-bend-left-up" />
              <span>
                A delegated run — <strong>{run.harness_name}</strong> did this work for another
                harness.
              </span>
              <button
                className="msg-action"
                onClick={() => onSelectRun(run.parent_run_id!)}
                title={`Open ${run.parent_run_id}`}
              >
                <i className="ph ph-arrow-square-out" />
                Open the parent run
              </button>
            </div>
          )}

          {(run.reason || hasEvidence(open)) && (
            <RunEvidence detail={open} trajectory={trajectory} onSelectRun={onSelectRun} />
          )}
        </div>

        <div className="trajectory-toolbar">
          <div className="filter-row">
            {FILTERS.map((item) => {
              const count =
                item.id === 'all'
                  ? open.events.length
                  : open.events.filter((event) => categoryOf(event.type) === item.id).length
              return (
                <button
                  key={item.id}
                  className="trajectory-filter"
                  data-on={filter === item.id ? '1' : '0'}
                  onClick={() => setFilter(item.id)}
                >
                  {item.label} <span>{count}</span>
                </button>
              )
            })}
          </div>
          <div className="fold-row">
            <button
              className="trajectory-toggle"
              data-on={pairCalls ? '1' : '0'}
              onClick={() => setPairCalls((value) => !value)}
              title="Fold each call and its result into one row"
            >
              <i className="ph ph-arrows-in-line-horizontal" />
              Calls
            </button>
            <button
              className="trajectory-toggle"
              data-on={groupTurns ? '1' : '0'}
              onClick={() => setGroupTurns((value) => !value)}
              title="Mark turn boundaries in the ledger"
            >
              <i className="ph ph-rows" />
              Turns
            </button>
          </div>
          <label className="trajectory-search">
            <i className="ph ph-magnifying-glass" />
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search event payloads"
            />
          </label>
        </div>

        {trajectory && (
          <Timeline
            trajectory={trajectory}
            events={open.events}
            selectedSeq={selected?.seq ?? null}
            onSelect={(seq) => {
              const event = eventBySeq.get(seq)
              if (event) setSelected(event)
            }}
          />
        )}

        <div className="trajectory-workspace">
          <div className="ledger" role="list" aria-label="Run trajectory">
            {visibleEvents.map((event, index) => {
              const previous = index > 0 ? visibleEvents[index - 1] : null
              const turn = trajectory?.turnBySeq.get(event.seq) ?? null
              const previousTurn = previous ? trajectory?.turnBySeq.get(previous.seq) ?? null : null
              const span = trajectory?.spanBySeq.get(event.seq) ?? null
              return (
                <div key={event.seq}>
                  <EventRow
                    event={event}
                    previous={previous}
                    turnLabel={
                      groupTurns && turn !== null && turn !== previousTurn ? `Turn ${turn}` : null
                    }
                    span={span && span.startSeq === event.seq ? span : null}
                    result={
                      pairCalls &&
                      span &&
                      span.startSeq === event.seq &&
                      span.endSeq !== null &&
                      span.endSeq !== span.startSeq
                        ? eventBySeq.get(span.endSeq) ?? null
                        : null
                    }
                    selected={selected?.seq === event.seq}
                    forkable={open.fork_points.some((point) => point.seq === event.seq)}
                    onSelect={() => setSelected(event)}
                  />
                </div>
              )
            })}
            {visibleEvents.length === 0 && (
              <div className="empty compact">No events match this filter.</div>
            )}
          </div>

          <EventInspector
            runId={run.run_id}
            event={selected}
            onClose={() => setSelected(null)}
            span={selected ? trajectory?.spanBySeq.get(selected.seq) ?? null : null}
            pair={pairEventFor(selected, trajectory, eventBySeq)}
            previous={previousEventFor(selected, open.events)}
            forkPoint={
              selected ? open.fork_points.find((point) => point.seq === selected.seq) ?? null : null
            }
            onFork={setForking}
          />
        </div>

        {trajectory && <RunTotals trajectory={trajectory} />}

        {forking && (
          <ForkDialog
            runId={run.run_id}
            point={forking}
            onClose={() => setForking(null)}
            onOpenHarness={async (id) => {
              setForking(null)
              await onOpenHarness(id)
            }}
          />
        )}
      </div>
    )
  }

  return (
    <div className="pane">
      {harness.stats && harness.stats.total_runs > 0 && (
        <div className="v-panel" style={{ padding: 20, marginBottom: 20 }}>
          <Label>Fitness</Label>
          <div style={{ marginTop: 14 }}>
            <StatRow>
              <Stat label="runs" value={String(harness.stats.total_runs)} />
              <Stat
                label="success"
                value={`${Math.round(harness.stats.success_rate * 100)}%`}
                color={harness.stats.success_rate >= 0.8 ? 'var(--ok)' : 'var(--warn)'}
              />
              <Stat label="avg cost" value={`$${harness.stats.avg_cost_usd.toFixed(4)}`} />
              <Stat label="avg turns" value={harness.stats.avg_turns.toFixed(1)} />
            </StatRow>
          </div>

          {(harness.stats.failure_signatures?.verdicts ?? []).length > 0 && (
            <div style={{ marginTop: 22 }}>
              <Label>Recurring failures</Label>
              <div style={{ marginTop: 10, display: 'grid', gap: 6 }}>
                {harness.stats.failure_signatures.verdicts!.map((sig) => (
                  <div key={sig.feedback} style={{ display: 'flex', gap: 10, alignItems: 'baseline' }}>
                    <span className="mono" style={{ color: 'var(--err)', fontSize: 12 }}>
                      {sig.count}×
                    </span>
                    <span style={{ color: 'var(--dim)', fontSize: 13 }}>{sig.feedback}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {runs === null ? (
        <div className="empty">Loading…</div>
      ) : runs.length === 0 ? (
        <div className="empty">
          <i className="ph ph-tray" style={{ fontSize: 26, display: 'block', marginBottom: 10 }} />
          No traces on disk for this harness yet.
        </div>
      ) : (
        <table className="grid">
          <thead>
            <tr>
              <th>run</th>
              <th>status</th>
              <th>when</th>
              <th>turns</th>
              <th>cost</th>
              <th>duration</th>
              <th>version</th>
              <th>lineage</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <tr key={run.run_id} className="v-row" onClick={() => onSelectRun(run.run_id)}>
                <td
                  style={{ fontSize: 12, color: 'var(--text)' }}
                  title={[run.run_id, run.task].filter(Boolean).join(' · ')}
                >
                  {runLabel(run)}
                </td>
                <td><StatusPill status={run.status} /></td>
                <td>{when(run.started_at)}</td>
                <td>{run.turns}</td>
                <td>${run.cost_usd.toFixed(4)}</td>
                <td>{run.duration_seconds.toFixed(1)}s</td>
                <td className="mono" style={{ fontSize: 12 }}>{run.harness_version_hash.slice(0, 12)}</td>
                <td>{run.parent_run_id ? `fork @ ${run.forked_at_seq}` : 'root'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

function EventRow({
  event,
  previous,
  turnLabel,
  span,
  result,
  selected,
  forkable,
  onSelect,
}: {
  event: TraceEvent
  previous: TraceEvent | null
  /** Rendered in the left margin, on the first row of each turn only. */
  turnLabel: string | null
  /** Set when this event *opens* a span, so the row can show its duration. */
  span: Span | null
  /** The folded-away closing half, previewed inline. */
  result: TraceEvent | null
  selected: boolean
  forkable: boolean
  onSelect: () => void
}) {
  const category = categoryOf(event.type)
  const open = span !== null && span.endSeq === null
  return (
    <button
      type="button"
      role="listitem"
      className="event-row"
      data-on={selected ? '1' : '0'}
      onClick={onSelect}
    >
      <span className="event-turn mono">{turnLabel}</span>
      <span className="event-seq mono">{String(event.seq).padStart(3, '0')}</span>
      <span className={`event-kind cat-${category}`}>{kindLabel(event.type)}</span>
      <span className="event-main">
        <strong>{eventTitle(event)}</strong>
        <span>{eventSummary(event)}</span>
        {result && (
          <span className={`event-result${resultFailed(result) ? ' failed' : ''}`}>
            <i className="ph ph-arrow-right" />
            {resultPreview(result)}
          </span>
        )}
        {span && span.updateSeqs.length > 0 && (
          <span className="event-updates" title={`${span.updateSeqs.length} update/retry events`}>
            +{span.updateSeqs.length}
          </span>
        )}
      </span>
      {forkable && <span className="fork-mark" title="Safe fork boundary"><i className="ph ph-git-fork" /></span>}
      {span ? (
        <span
          className="event-delta mono duration"
          data-open={open ? '1' : '0'}
          title={open ? 'The journal never recorded this call closing' : 'Paired call duration'}
        >
          {open ? 'open' : formatMs(span.durationMs)}
        </span>
      ) : (
        <span className="event-delta mono" title="Time since the previous event">
          {delta(previous, event)}
        </span>
      )}
    </button>
  )
}

function resultPreview(result: TraceEvent): string {
  const payload = result.payload
  for (const key of ['content', 'text', 'output', 'error']) {
    if (payload[key] !== undefined && payload[key] !== '') return preview(payload[key], 120)
  }
  if (Array.isArray(payload.tool_calls) && payload.tool_calls.length > 0) {
    return `calls ${payload.tool_calls.join(', ')}`
  }
  return preview(payload.stop_reason ?? payload, 120)
}

function resultFailed(result: TraceEvent): boolean {
  const value = result.payload.is_error
  return value === true || value === 'True' || value === 'true'
}

function pairEventFor(
  event: TraceEvent | null,
  trajectory: TrajectoryProjection | null,
  eventBySeq: Map<number, TraceEvent>,
): TraceEvent | null {
  if (!event || !trajectory) return null
  const span = trajectory.spanBySeq.get(event.seq)
  if (!span || span.endSeq === null) return null
  const other = span.startSeq === event.seq ? span.endSeq : span.startSeq
  if (other === event.seq) return null
  return eventBySeq.get(other) ?? null
}

function previousEventFor(event: TraceEvent | null, events: TraceEvent[]): TraceEvent | null {
  if (!event) return null
  const index = events.findIndex((item) => item.seq === event.seq)
  return index > 0 ? events[index - 1] : null
}

function EventInspector({
  runId,
  event,
  span,
  pair,
  previous,
  forkPoint,
  onClose,
  onFork,
}: {
  runId: string
  event: TraceEvent | null
  span: Span | null
  pair: TraceEvent | null
  previous: TraceEvent | null
  /** Set when this event is a boundary the run can be re-entered at. */
  forkPoint: ForkPoint | null
  onClose: () => void
  onFork: (point: ForkPoint) => void
}) {
  const [tab, setTab] = useState<InspectorTab>('summary')
  const [context, setContext] = useState<MaterializedContext | null>(null)
  const [contextError, setContextError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  const toolName = event ? (event.payload.name as string | undefined) : undefined
  const tabs = useMemo<InspectorTab[]>(() => {
    const list: InspectorTab[] = ['summary', 'payload']
    if (pair) list.push('result')
    list.push('context')
    if (event && categoryOf(event.type) === 'tool' && toolName) list.push('schema')
    if (span) list.push('timing')
    list.push('raw')
    return list
  }, [event, pair, span, toolName])

  useEffect(() => {
    setTab('summary')
    setContext(null)
    setContextError(null)
  }, [event?.seq, runId])

  // Context and Schema read the same fold: the tools a model call actually saw.
  useEffect(() => {
    if ((tab !== 'context' && tab !== 'schema') || !event || context || loading) return
    setLoading(true)
    api
      .context(runId, event.seq)
      .then(setContext)
      .catch((exc) => setContextError(String(exc)))
      .finally(() => setLoading(false))
  }, [context, event, loading, runId, tab])

  if (!event) return null

  return (
    <aside className="event-inspector">
      <header>
        <span className={`event-kind cat-${categoryOf(event.type)}`}>{kindLabel(event.type)}</span>
        <div>
          <strong>{eventTitle(event)}</strong>
          <div className="mono inspector-sub">seq {event.seq} · {formatTimestamp(event.timestamp)}</div>
        </div>
        <button className="inspector-close" onClick={onClose} title="Close the inspector">
          <i className="ph ph-x" />
        </button>
      </header>

      <nav className="inspector-tabs">
        {tabs.map((item) => (
          <button key={item} data-on={tab === item ? '1' : '0'} onClick={() => setTab(item)}>
            {item === 'context' ? 'Context at event' : item}
          </button>
        ))}
      </nav>

      {forkPoint && (
        <div className="fork-strip">
          <i className="ph ph-git-fork" />
          <span>
            Safe boundary — re-enter the run here with one variable changed.
          </span>
          <button
            className="v-btn v-btn-ghost v-btn-sm"
            title="Copy the harness folder and re-enter the run there"
            onClick={() => onFork(forkPoint)}
          >
            Fork the harness…
          </button>
        </div>
      )}

      <div className="inspector-body">
        {tab === 'summary' && <EventSummary event={event} />}
        {tab === 'payload' && <JsonBlock value={event.payload} />}
        {tab === 'result' && <ResultView event={event} pair={pair} />}
        {tab === 'raw' && <JsonBlock value={event} />}
        {tab === 'timing' && <TimingView event={event} span={span} previous={previous} />}
        {tab === 'schema' && (
          <SchemaView
            context={context}
            loading={loading}
            error={contextError}
            toolName={toolName}
          />
        )}
        {tab === 'context' && (
          <ContextView context={context} loading={loading} error={contextError} event={event} />
        )}
      </div>
    </aside>
  )
}

/** The other half of the pair: what the call actually returned. */
function ResultView({ event, pair }: { event: TraceEvent; pair: TraceEvent | null }) {
  if (!pair) return <div className="empty compact">This event has no paired result.</div>
  const payload = pair.payload
  const failed = resultFailed(pair)
  const body = payload.content ?? payload.text ?? ''
  const opening = pair.seq > event.seq
  return (
    <>
      <div className={`inspector-callout${failed ? ' bad' : ''}`}>
        {failed ? 'The tool returned an error.' : opening ? 'Returned at' : 'Opened at'} seq {pair.seq}
        {payload.stop_reason ? ` · stop_reason ${String(payload.stop_reason)}` : ''}
      </div>
      {typeof body === 'string' && body !== '' ? (
        <pre className="json-block">{body}</pre>
      ) : (
        <JsonBlock value={payload} />
      )}
      {Array.isArray(payload.tool_calls) && payload.tool_calls.length > 0 && (
        <dl className="event-fields">
          <div>
            <dt>tool calls</dt>
            <dd className="mono">{payload.tool_calls.join(', ')}</dd>
          </div>
        </dl>
      )}
    </>
  )
}

/** The schema the executor was actually offered for this tool at this point. */
function SchemaView({
  context,
  loading,
  error,
  toolName,
}: {
  context: MaterializedContext | null
  loading: boolean
  error: string | null
  toolName?: string
}) {
  if (loading) return <div className="empty compact">Folding journal…</div>
  if (error) return <Notice icon="ph-warning" tone="err" title="Schema unavailable" body={error} />
  if (!context) return null
  if (!context.available) {
    return (
      <Notice
        icon="ph-clock-counter-clockwise"
        tone="warn"
        title="Tool schemas not recorded"
        body="This legacy journal carries no tool definitions at this boundary, so the schema the model saw cannot be reconstructed. Re-run it on the current journal format."
      />
    )
  }
  const tool = context.request.tools.find((item) => String(item.name) === toolName)
  if (!tool) {
    return (
      <Notice
        icon="ph-question"
        tone="warn"
        title={`${toolName} is not in the active tool set`}
        body="The journal's tool definitions at this boundary do not include this tool. It may have been added or removed by a playbook switch or a context hook."
      />
    )
  }
  return <JsonBlock value={tool} />
}

/** Where this event sat in the run's clock — measured, never inferred. */
function TimingView({
  event,
  span,
  previous,
}: {
  event: TraceEvent
  span: Span | null
  previous: TraceEvent | null
}) {
  const usage = span?.usage
  return (
    <>
      <div className="inspector-callout">
        {span === null
          ? 'A point event: the journal records when it happened, not how long it took.'
          : span.endSeq === null
            ? 'This call never closed in the journal — no duration can be claimed.'
            : `Paired with seq ${span.endSeq}; the duration is the gap between the two recorded timestamps.`}
      </div>
      <ContextMeter event={event} />
      <dl className="event-fields">
        <div>
          <dt>started</dt>
          <dd className="mono">{formatTimestamp(event.timestamp)}</dd>
        </div>
        {span?.endMs !== null && span?.durationMs !== null && span !== null && (
          <div>
            <dt>duration</dt>
            <dd className="mono">{formatMs(span.durationMs)}</dd>
          </div>
        )}
        <div>
          <dt>since previous</dt>
          <dd className="mono">{delta(previous, event)}</dd>
        </div>
        {span?.turn !== null && span !== null && (
          <div>
            <dt>turn</dt>
            <dd className="mono">{span.turn}</dd>
          </div>
        )}
        {usage && (
          <>
            <div>
              <dt>input tokens</dt>
              <dd className="mono">{formatTokens(usage.input_tokens)}</dd>
            </div>
            <div>
              <dt>output tokens</dt>
              <dd className="mono">{formatTokens(usage.output_tokens)}</dd>
            </div>
            {usage.cache_read_tokens > 0 && (
              <div>
                <dt>cache read</dt>
                <dd className="mono">{formatTokens(usage.cache_read_tokens)}</dd>
              </div>
            )}
            {usage.cache_write_tokens > 0 && (
              <div>
                <dt>cache write</dt>
                <dd className="mono">{formatTokens(usage.cache_write_tokens)}</dd>
              </div>
            )}
          </>
        )}
        {span?.costUsd !== undefined && (
          <div>
            <dt>cost</dt>
            <dd className="mono">${span.costUsd.toFixed(4)}</dd>
          </div>
        )}
      </dl>
    </>
  )
}

function EventSummary({ event }: { event: TraceEvent }) {
  const entries = Object.entries(event.payload).filter(([, value]) => isScalar(value))
  return (
    <>
      <div className="inspector-callout">{eventNarrative(event)}</div>
      {entries.length > 0 && (
        <dl className="event-fields">
          {entries.map(([key, value]) => (
            <div key={key}>
              <dt>{key.replaceAll('_', ' ')}</dt>
              <dd className={typeof value === 'number' ? 'mono' : ''}>{String(value)}</dd>
            </div>
          ))}
        </dl>
      )}
      {event.prev !== undefined && (
        <div className="chain-link">
          <Label>Previous journal hash</Label>
          <div className="mono">{event.prev || 'genesis'}</div>
        </div>
      )}
    </>
  )
}

function ContextView({
  context,
  loading,
  error,
  event,
}: {
  context: MaterializedContext | null
  loading: boolean
  error: string | null
  event: TraceEvent
}) {
  if (loading) return <div className="empty compact">Folding journal…</div>
  if (error) return <Notice icon="ph-warning" tone="err" title="Context unavailable" body={error} />
  if (!context) return null
  if (!context.available) {
    return (
      <Notice
        icon="ph-clock-counter-clockwise"
        tone="warn"
        title="Context not recorded"
        body="This legacy journal predates progressive context events and carries no request snapshot at this boundary. Re-run it on the current journal format to inspect the exact request."
      />
    )
  }
  return (
    <>
      <div className={`context-faithful ${context.faithful ? 'ok' : 'warn'}`}>
        <i className={`ph ${context.faithful ? 'ph-check-circle' : 'ph-warning'}`} />
        {context.faithful
          ? event.type === 'model_call'
            ? 'Exact request reconstructed from the journal.'
            : 'Journal state at this event.'
          : 'A context hook changed the wire request without persisting the patch.'}
      </div>
      <ContextSection label="System" value={context.request.system || '(empty)'} />
      <ContextSection label={`Messages · ${context.request.messages.length}`} value={context.request.messages} />
      <ContextSection label={`Tools · ${context.request.tools.length}`} value={context.request.tools} />
    </>
  )
}

function ContextSection({ label, value }: { label: string; value: unknown }) {
  return (
    <details className="context-section" open>
      <summary>{label}</summary>
      {typeof value === 'string' ? <pre>{value}</pre> : <JsonBlock value={value} />}
    </details>
  )
}

function RunEvidence({
  detail,
  trajectory,
  onSelectRun,
}: {
  detail: RunDetail
  trajectory: TrajectoryProjection | null
  onSelectRun: (runId: string | null) => void
}) {
  const run = detail.run
  const ancestors = detail.lineage.ancestors
  // `children` is the Hive's own name for the whole set; `forks` is the same
  // list under the name it had before delegated runs joined it. Prefer the
  // former and fall back, so an older API answer still renders.
  const children = detail.lineage.children ?? detail.lineage.forks
  const delegations = detail.delegations ?? []
  const referrals = detail.referrals ?? []
  const steps = trajectory?.delegations ?? []
  return (
    <details className="run-evidence">
      <summary>
        <i className="ph ph-magnifying-glass" />
        Evidence
        <span>{run.verifications?.length ?? 0} verdicts · {run.guardrail_triggers?.length ?? 0} guardrails · {detail.artifacts.length} artifacts</span>
      </summary>
      <div className="evidence-grid">
        <section>
          <Label>Outcome</Label>
          <p>{run.reason || 'The run completed without an additional reason.'}</p>
          {run.verifications?.map((item) => (
            <div key={`${item.seq}-${item.verifier}`} className="evidence-line">
              <i className={`ph ${item.passed ? 'ph-check-circle' : 'ph-x-circle'}`} style={{ color: item.passed ? 'var(--ok)' : 'var(--err)' }} />
              <span><strong>{item.verifier}</strong>{item.feedback ? ` — ${item.feedback}` : ''}</span>
            </div>
          ))}
          {run.guardrail_triggers?.map((item) => (
            <div key={`${item.seq}-${item.guardrail}`} className="evidence-line">
              <i className="ph ph-shield-warning" style={{ color: 'var(--warn)' }} />
              <span><strong>{item.guardrail}</strong> — {item.reason || item.kind}</span>
            </div>
          ))}
        </section>
        <section>
          <Label>Lineage</Label>
          <p>{lineageLine(run)}</p>
          {ancestors.map((item) => (
            <button
              className="mono evidence-line evidence-link"
              key={item.run_id}
              onClick={() => onSelectRun(item.run_id)}
              title={`Open ${item.run_id}`}
            >
              ↑ {item.run_id}
            </button>
          ))}
          {children.map((item) => (
            <button
              className="mono evidence-line evidence-link"
              key={item.run_id}
              onClick={() => onSelectRun(item.run_id)}
              title={`Open ${item.run_id}`}
            >
              ↳ {item.run_id}
              {item.lineage_kind === 'delegation'
                ? ` · delegated to ${item.harness_name}`
                : ` @ ${item.forked_at_seq}`}
            </button>
          ))}
          {detail.fork_points.length > 0 && <p>{detail.fork_points.length} safe model-call fork boundaries.</p>}
        </section>
        {(delegations.length > 0 || referrals.length > 0 || steps.length > 0) && (
          <section>
            <Label>Delegation</Label>
            {(detail.delegated_cost_usd ?? 0) > 0 && (
              <p>
                ${(detail.delegated_cost_usd ?? 0).toFixed(4)} of this run's cost was spent by a
                peer, not by this harness.
              </p>
            )}
            {delegations.map((record, index) => (
              <div className="evidence-line" key={`${record.harness}-${record.run_id || index}`}>
                <i className="ph ph-arrow-bend-up-right" />
                <span>
                  <strong>{record.harness}</strong> · {record.status || 'no status'} · $
                  {(record.cost_usd ?? 0).toFixed(4)} · {record.turns ?? 0} turns
                  {record.reason ? ` — ${record.reason}` : ''}
                </span>
                {record.run_id && (
                  <button
                    className="msg-action"
                    onClick={() => onSelectRun(record.run_id)}
                    title={`Open ${record.run_id}`}
                  >
                    Open run
                  </button>
                )}
              </div>
            ))}
            {referrals.map((referral, index) => (
              <div className="evidence-line" key={`${referral.harness}-${index}`}>
                <i className="ph ph-signpost" />
                <span title={referral.description}>
                  <strong>{referral.harness}</strong> · success{' '}
                  {Math.round((referral.success_rate ?? 0) * 100)}% over {referral.total_runs ?? 0}{' '}
                  runs · {referral.reason}
                </span>
              </div>
            ))}
            {delegations.length === 0 && referrals.length === 0 && (
              <p>{steps.length} hand-off decision{steps.length === 1 ? '' : 's'} in the journal.</p>
            )}
          </section>
        )}
        <section>
          <Label>Artifacts</Label>
          {detail.artifacts.length === 0 ? (
            <p>No structured artifacts.</p>
          ) : (
            detail.artifacts.map((artifact, index) => (
              <ArtifactRow key={`${artifact.kind}-${index}`} artifact={artifact} />
            ))
          )}
        </section>
      </div>
    </details>
  )
}

/**
 * How close this call ran to its context budget.
 *
 * Both numbers are recorded on the `model_call` itself, so this is measured
 * rather than re-tokenized here — and a journal that predates the meter simply
 * renders nothing.
 */
function ContextMeter({ event }: { event: TraceEvent }) {
  const used = Number(event.payload.input_tokens)
  const budget = Number(event.payload.max_input_tokens)
  if (!Number.isFinite(used) || !Number.isFinite(budget) || budget <= 0) return null
  const share = Math.min(1, used / budget)
  const tone = share > 0.9 ? 'bad' : share > 0.7 ? 'warn' : 'ok'
  return (
    <div className="meter">
      <div className="meter-head">
        <span>context</span>
        <span className="mono">
          {formatTokens(used)} / {formatTokens(budget)} · {Math.round(share * 100)}%
        </span>
      </div>
      <div className="meter-track">
        <div className={`meter-fill ${tone}`} style={{ width: `${share * 100}%` }} />
      </div>
    </div>
  )
}

function JsonBlock({ value }: { value: unknown }) {
  return <pre className="json-block">{JSON.stringify(value, null, 2)}</pre>
}

/**
 * Artifacts render by `kind`, with JSON as the fallback.
 *
 * A registry rather than a switch so a new kind is one entry, and so an
 * unknown kind still shows its data instead of disappearing.
 */
const ARTIFACTS: Record<string, (data: Record<string, unknown>) => React.ReactNode> = {
  file: (data) => (
    <span>
      <strong>{String(data.path ?? 'file')}</strong>
      {data.action ? ` · ${String(data.action)}` : ''}
      {typeof data.size === 'number' ? ` · ${formatBytes(data.size)}` : ''}
      {data.sha256 ? (
        <span className="mono artifact-hash" title={String(data.sha256)}>
          {String(data.sha256).slice(0, 12)}
          {data.previous_sha256 ? ` ← ${String(data.previous_sha256).slice(0, 12)}` : ''}
        </span>
      ) : null}
    </span>
  ),
}

function ArtifactRow({ artifact }: { artifact: Artifact }) {
  const data = asRecord(artifact.data)
  const render = ARTIFACTS[artifact.kind]
  return (
    <div className="evidence-line">
      <i className={`ph ${artifact.kind === 'file' ? 'ph-file-text' : 'ph-package'}`} />
      {render ? (
        render(data)
      ) : (
        <span>
          <strong>{artifact.kind}</strong> · {preview(artifact.data, 90)}
        </span>
      )}
    </div>
  )
}

function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${(size / (1024 * 1024)).toFixed(1)} MB`
}

function hasEvidence(detail: RunDetail): boolean {
  return Boolean(
    detail.run.reason ||
      detail.run.verifications?.length ||
      detail.run.guardrail_triggers?.length ||
      detail.artifacts.length ||
      detail.lineage.ancestors.length ||
      detail.lineage.forks.length ||
      detail.lineage.children?.length ||
      detail.delegations?.length ||
      detail.referrals?.length,
  )
}

/** How this run hangs off its parent, in the vocabulary of the kind it is. */
function lineageLine(run: RunRow): string {
  if (!run.parent_run_id) return 'Root run.'
  if (run.lineage_kind === 'delegation') {
    return `Delegated by ${run.parent_run_id} — this run did that run's work.`
  }
  return `Forked from ${run.parent_run_id} at seq ${run.forked_at_seq}.`
}

/**
 * The one-line delegation headline for the metrics row.
 *
 * The recorded receipts first — they are what the run was charged for — and
 * the journal's own decisions when there are none, so a run that only ever
 * *considered* a hand-off still says so.
 */
function delegationStat(
  detail: RunDetail,
  trajectory: TrajectoryProjection | null,
): string | null {
  const delegations = detail.delegations ?? []
  const cost = detail.delegated_cost_usd ?? 0
  if (delegations.length > 0) {
    const peers = [...new Set(delegations.map((record) => record.harness))].join(', ')
    return `${peers} · $${cost.toFixed(4)}`
  }
  if (detail.run.lineage_kind === 'delegation') return 'delegated run'
  const steps = trajectory?.delegations ?? []
  if (steps.length > 0) return `${steps.length} considered`
  if ((detail.referrals ?? []).length > 0) return `${detail.referrals!.length} referred`
  return null
}

function kindLabel(type: string): string {
  const category = categoryOf(type)
  if (type === 'model_response') return 'assistant'
  if (type === 'context_append') return 'message'
  if (type === 'verification_result') return 'verifier'
  // 'delegation' does not fit the ledger's kind column; the verb does.
  if (category === 'delegation') return 'delegate'
  return category
}

function eventTitle(event: TraceEvent): string {
  const p = event.payload
  switch (event.type) {
    case 'run_started': return 'Run started'
    case 'run_finished': return `Run ${String(p.status ?? 'finished')}`
    case 'model_call': return `Model call · turn ${String(p.turn ?? '?')}`
    case 'model_response': return `Model response · turn ${String(p.turn ?? '?')}`
    case 'tool_call': return String(p.name ?? 'Tool call')
    case 'tool_result': return `${String(p.name ?? 'Tool')} result`
    case 'tool_update': return `${String(p.name ?? 'Tool')} update`
    case 'tool_retry': return `${String(p.name ?? 'Tool')} retry`
    case 'tool_spilled': return `${String(p.name ?? 'Tool')} result spilled`
    case 'spill_inherited': return 'Spilled results inherited'
    case 'verification_result': return String(p.verifier ?? 'Verification')
    case 'delegation_selected': return `Peer chosen · ${String(p.harness ?? '?')}`
    case 'delegation_started': return `Delegating to ${String(p.harness ?? '?')}`
    case 'delegation_finished':
      return `${String(p.harness ?? 'Peer')} ${String(p.status ?? 'finished')}`
    case 'delegation_skipped': return 'No hand-off'
    case 'guardrail_triggered': return String(p.guardrail ?? 'Guardrail triggered')
    case 'context_append': {
      const message = asRecord(p.message)
      return `${String(message.role ?? 'context')} message`
    }
    case 'context_system': return 'System prompt changed'
    case 'context_tools': return 'Tool schema changed'
    case 'context_compaction': return 'Context compacted'
    case 'playbook_switch': return `Playbook → ${String(p.to ?? '?')}`
    case 'model_swap': return `Model → ${String(p.model ?? p.to ?? '?')}`
    case 'user_steer': return 'Operator message'
    default: return event.type.replaceAll('_', ' ')
  }
}

function eventSummary(event: TraceEvent): string {
  const p = event.payload
  // Before the generic scan below: every delegation event carries `reason` or
  // `status`, which the scan would return on its own and drop the rest.
  if (categoryOf(event.type) === 'delegation') return delegationSummary(event)
  if (event.type === 'context_append') {
    const message = asRecord(p.message)
    return preview(message.content ?? message.text ?? '', 180)
  }
  for (const key of ['text', 'content', 'input', 'output', 'feedback', 'reason', 'error', 'status']) {
    if (p[key] !== undefined && p[key] !== '') return preview(p[key], 180)
  }
  if (event.type === 'tool_call') return preview(p.input, 180)
  if (event.type === 'model_call') {
    return `${String(p.phase ?? 'act')} phase · ${String(p.num_messages ?? '?')} messages`
  }
  if (event.type === 'tool_spilled') {
    return `${String(p.omitted_bytes ?? '?')} of ${String(p.bytes ?? '?')} bytes kept out of context as ${String(p.handle ?? '?')}`
  }
  if (event.type === 'context_tools') {
    return `${Array.isArray(p.tools) ? p.tools.length : '?'} active tool definitions`
  }
  return preview(p, 180)
}

/**
 * One delegation event, said in full.
 *
 * Each of the four answers a different question — who was chosen and on what
 * measured odds, how deep the hand-off is and what it may spend, what the child
 * produced, or why nothing was handed over at all — so each gets its own line
 * rather than a shared field scan.
 */
function delegationSummary(event: TraceEvent): string {
  const p = event.payload
  const mode = p.mode ? `${String(p.mode)} · ` : ''
  switch (event.type) {
    case 'delegation_selected': {
      const runs = Number(p.total_runs)
      const rate = Number(p.success_rate)
      const fitness = Number.isFinite(rate) && Number.isFinite(runs)
        ? `success ${Math.round(rate * 100)}% over ${runs} runs`
        : 'no measured fitness'
      return `${mode}${String(p.harness ?? '?')} · ${fitness}`
    }
    case 'delegation_started': {
      const chain = Array.isArray(p.chain) ? p.chain.length : 0
      const cap = Number(p.cost_cap_usd)
      const budget = Number.isFinite(cap) ? `cap $${cap.toFixed(4)}` : 'no extra cap'
      return `${mode}depth ${String(p.depth ?? '?')} · chain of ${chain} · ${budget}`
    }
    case 'delegation_finished': {
      const cost = Number(p.cost_usd)
      const spent = Number.isFinite(cost) ? ` · $${cost.toFixed(4)}` : ''
      const turns = p.turns === undefined ? '' : ` · ${String(p.turns)} turns`
      const run = p.run_id ? ` · ${String(p.run_id)}` : ''
      const why = p.reason || p.error ? ` — ${String(p.reason ?? p.error)}` : ''
      return `${mode}${String(p.status ?? '?')}${spent}${turns}${run}${why}`
    }
    default: {
      const who = p.harness ? `${String(p.harness)} · ` : ''
      return `${mode}${who}${String(p.reason ?? 'no reason recorded')}`
    }
  }
}

function eventNarrative(event: TraceEvent): string {
  const summary = eventSummary(event)
  switch (categoryOf(event.type)) {
    case 'model': return summary || 'The executor crossed a model boundary.'
    case 'tool': return summary || 'A tool changed the state available to the executor.'
    case 'context': return summary || 'The folded model context changed.'
    case 'verify': return summary || 'A verifier judged the final output.'
    case 'safety': return summary || 'A safety or lifecycle hook intervened.'
    case 'delegation':
      return summary || 'The run looked for a peer to hand this task to.'
    case 'control': return summary || 'The harness changed execution policy.'
    default: return summary || 'The run lifecycle advanced.'
  }
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {}
}

function isScalar(value: unknown): value is string | number | boolean | null {
  return value === null || ['string', 'number', 'boolean'].includes(typeof value)
}

function delta(previous: TraceEvent | null, event: TraceEvent): string {
  if (!previous) return '0ms'
  const milliseconds = Date.parse(event.timestamp) - Date.parse(previous.timestamp)
  if (!Number.isFinite(milliseconds) || milliseconds < 0) return '—'
  if (milliseconds < 1000) return `${Math.round(milliseconds)}ms`
  return `${(milliseconds / 1000).toFixed(milliseconds < 10_000 ? 2 : 1)}s`
}

function formatTimestamp(value: string): string {
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleTimeString()
}
