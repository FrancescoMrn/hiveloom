import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import { loadPrefs } from '../prefs'
import type {
  Harness,
  HarnessDetail,
  HarnessInterface,
  RunRow,
  VersionTags,
} from '../types'
import { Evolve } from './Evolve'
import { InterfacePreview } from './CopilotCanvas'
import { SpecEditor } from './SpecEditor'
import { Trajectory } from './Trajectory'
import { Versions } from './Versions'
import { Label, Notice, Stat, StatRow } from './common'

export type InspectorView =
  | 'overview'
  | 'runs'
  | 'trace'
  | 'versions'
  | 'spec'
  | 'improve'
  | 'interface'

const VIEWS: { id: InspectorView; label: string; icon: string }[] = [
  { id: 'interface', label: 'Use', icon: 'ph-play-circle' },
  { id: 'overview', label: 'Overview', icon: 'ph-info' },
  { id: 'runs', label: 'Runs', icon: 'ph-list-magnifying-glass' },
  { id: 'trace', label: 'Trace', icon: 'ph-path' },
  { id: 'versions', label: 'Versions', icon: 'ph-git-branch' },
  { id: 'spec', label: 'Spec', icon: 'ph-file-code' },
  { id: 'improve', label: 'Improve', icon: 'ph-sparkle' },
]

/**
 * Deterministic counterpart to the copilot conversation.
 *
 * Chat can explain or act on these facts, but it is never the only route to
 * them: selecting a harness or run opens the exact spec, journal, versions,
 * proposals, and generated interface here.
 */
export function ContextInspector({
  harnessId,
  harnesses,
  runs,
  runId,
  expanded,
  requestedView,
  requestedViewKey,
  onToggleExpanded,
  onClose,
  onSelectRun,
  onOpenHarness,
  onRefresh,
}: {
  harnessId: string
  harnesses: Harness[]
  runs: RunRow[] | null
  runId: string | null
  expanded: boolean
  requestedView?: InspectorView
  requestedViewKey?: number
  onToggleExpanded: () => void
  onClose: () => void
  onSelectRun: (runId: string | null) => void
  onOpenHarness: (harnessId: string) => Promise<void>
  onRefresh: () => Promise<void>
}) {
  const [view, setView] = useState<InspectorView>(
    runId ? 'trace' : requestedView ?? 'interface',
  )
  const [harness, setHarness] = useState<HarnessDetail | null>(null)
  const [tags, setTags] = useState<VersionTags>({})
  const [harnessInterface, setHarnessInterface] = useState<HarnessInterface | null>(null)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      const [detail, versionTags, interfaceRecord] = await Promise.all([
        api.harness(harnessId),
        api.tags(harnessId),
        api.harnessInterface(harnessId),
      ])
      setHarness(detail)
      setTags(versionTags)
      setHarnessInterface(interfaceRecord)
      setError(null)
    } catch (exc) {
      setError(String(exc))
    }
  }, [harnessId])

  useEffect(() => {
    setHarness(null)
    setView(runId ? 'trace' : requestedView ?? 'interface')
    void load()
  }, [harnessId, load])

  useEffect(() => {
    if (runId) setView('trace')
  }, [runId])

  useEffect(() => {
    if (requestedView) setView(requestedView)
    if (requestedView === 'interface') void load()
  }, [load, requestedView, requestedViewKey])

  const changed = async () => {
    await Promise.all([load(), onRefresh()])
  }

  return (
    <aside className="context-inspector" data-expanded={expanded ? '1' : '0'}>
      <header className="context-inspector-head">
        <div>
          <div className="v-label">Harness workspace</div>
          <h2>{harness?.name ?? harnessId}</h2>
        </div>
        <button
          className="icon-btn"
          onClick={onToggleExpanded}
          title={expanded ? 'Return to split view' : 'Expand inspector'}
        >
          <i className={`ph ${expanded ? 'ph-arrows-in' : 'ph-arrows-out'}`} />
        </button>
        <button className="icon-btn" onClick={onClose} title="Close inspector">
          <i className="ph ph-x" />
        </button>
      </header>

      <nav className="context-inspector-tabs" aria-label="Harness information">
        {VIEWS.map((item) => (
          <button
            key={item.id}
            data-on={view === item.id ? '1' : '0'}
            disabled={item.id === 'trace' && !runId}
            onClick={() => setView(item.id)}
          >
            <i className={`ph ${item.icon}`} />
            {item.label}
          </button>
        ))}
      </nav>

      <div className="context-inspector-body">
        {error ? (
          <Notice icon="ph-warning-octagon" tone="err" title="Could not load harness" body={error} />
        ) : !harness ? (
          <div className="empty">Loading harness information…</div>
        ) : view === 'overview' ? (
          <HarnessOverview harness={harness} />
        ) : view === 'runs' ? (
          <Trajectory
            harness={harness}
            runId={null}
            runs={runs}
            onSelectRun={(id) => {
              onSelectRun(id)
              if (id) setView('trace')
            }}
            onOpenHarness={onOpenHarness}
          />
        ) : view === 'trace' && runId ? (
          <Trajectory
            harness={harness}
            runId={runId}
            runs={runs}
            onSelectRun={(id) => {
              onSelectRun(id)
              if (!id) setView('runs')
            }}
            onOpenHarness={onOpenHarness}
          />
        ) : view === 'versions' ? (
          <Versions
            harness={harness}
            harnesses={harnesses}
            runs={runs}
            tags={tags}
            onTags={setTags}
            onBack={() => setView('overview')}
            onOpenRun={(id) => {
              onSelectRun(id)
              setView('trace')
            }}
            onOpenSpec={() => setView('spec')}
            onStartRun={(id) => void onOpenHarness(id)}
            embedded
          />
        ) : view === 'spec' ? (
          <SpecEditor harness={harness} onSaved={changed} />
        ) : view === 'improve' ? (
          <Evolve
            harness={harness}
            evolveModel={loadPrefs().evolveModel}
            onApplied={changed}
            onCompare={() => setView('versions')}
          />
        ) : view === 'interface' ? (
          harnessInterface?.exists ? (
            <InterfacePreview
              data={{
                ...harnessInterface,
                harness_id: harness.id,
                harness_name: harness.name,
              }}
              onChanged={changed}
              onOpenRun={(id) => {
                onSelectRun(id)
                setView('trace')
              }}
            />
          ) : (
            <div className="inspector-empty-action">
              <i className="ph ph-play-circle" />
              <h3>This harness does not have a Use interface yet</h3>
              <p>
                Ask the copilot to “create an interface for {harness.name}”. Once created,
                opening this harness will bring you directly here to run it.
              </p>
            </div>
          )
        ) : null}
      </div>
    </aside>
  )
}

function HarnessOverview({ harness }: { harness: HarnessDetail }) {
  const spec = record(harness.spec)
  const model = record(spec.model)
  const verify = record(spec.verify)
  const tools = array(spec.tools)
  const guardrails = array(spec.guardrails)
  return (
    <div className="harness-overview">
      <section className="overview-hero">
        <Label>Purpose</Label>
        <h3>{harness.name}</h3>
        <p>{harness.description}</p>
        <div className="overview-identifiers mono">
          <span>{portableHarnessPath(harness.path)}</span>
          <span>version {harness.version_hash?.slice(0, 12)}</span>
        </div>
      </section>

      {harness.stats && (
        <section className="v-panel overview-fitness">
          <Label>Recorded fitness</Label>
          <StatRow>
            <Stat label="runs" value={String(harness.stats.total_runs)} />
            <Stat label="success" value={`${Math.round(harness.stats.success_rate * 100)}%`} />
            <Stat label="avg cost" value={`$${harness.stats.avg_cost_usd.toFixed(4)}`} />
            <Stat label="avg turns" value={harness.stats.avg_turns.toFixed(1)} />
          </StatRow>
        </section>
      )}

      <div className="overview-grid">
        <OverviewBlock title="Model" value={`${model.provider ?? ''}/${model.id ?? ''}`} />
        <OverviewBlock title="Tools" value={tools} />
        <OverviewBlock title="Verification" value={verify} />
        <OverviewBlock title="Guardrails" value={guardrails} />
      </div>

      <section>
        <Label>System prompt</Label>
        <pre className="canvas-output overview-prompt">{String(spec.system_prompt ?? '')}</pre>
      </section>
    </div>
  )
}

function OverviewBlock({ title, value }: { title: string; value: unknown }) {
  return (
    <section className="v-panel overview-block">
      <Label>{title}</Label>
      <pre>{pretty(value)}</pre>
    </section>
  )
}

function portableHarnessPath(value: string): string {
  const path = value.replaceAll('\\', '/')
  const harnessesAt = path.lastIndexOf('/harnesses/')
  if (harnessesAt >= 0) return path.slice(harnessesAt + 1)
  if (!path.startsWith('/')) return path
  return path.split('/').filter(Boolean).at(-1) ?? 'harness'
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {}
}
function array(value: unknown): unknown[] { return Array.isArray(value) ? value : [] }
function pretty(value: unknown): string {
  return typeof value === 'string' ? value : JSON.stringify(value ?? null, null, 2)
}
