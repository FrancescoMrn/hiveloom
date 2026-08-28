/**
 * The version graph: every version of one harness, and what each one is.
 *
 * A harness is not a file you edit, it is a series of versions with evidence
 * attached, and the rest of the workbench only ever shows you one of them at a
 * time. This is the screen where they are all visible at once — the trunk it
 * evolved along, the forks that left it and were never promoted, which
 * conversations were pinned where, and how the runs on each version actually
 * went.
 *
 * Every node names its own source (see `lineage.ts`): an applied proposal, a
 * fork record, or nothing at all — which is what a hand edit looks like, and is
 * labelled as one rather than being attached to a parent it never had.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import { ancestryOf, buildVersionGraph, childrenOf, type VersionNode } from '../lineage'
import { bestRun } from '../runs'
import type {
  Comparison,
  Harness,
  HarnessDetail,
  Proposal,
  RunRow,
  VersionTags,
} from '../types'
import { Label, Notice, Stat, StatRow, StatusPill, statusColor, when } from './common'

type Tab = 'graph' | 'compare'

/** Row geometry, shared by the rows and the edges drawn behind them. */
const ROW_HEIGHT = 78
const ROW_CENTRE = 39
const LANE_STEP = 26
const LANE_ORIGIN = 22

const KIND_COLOR: Record<string, string> = {
  initial: 'var(--mut)',
  evolved: 'var(--acc)',
  fork: 'var(--evo)',
  edited: 'var(--dim)',
}

export function Versions({
  harness,
  harnesses,
  runs,
  tags,
  onTags,
  onBack,
  onOpenRun,
  onOpenSpec,
  onStartRun,
  initialTab = 'graph',
  embedded = false,
}: {
  harness: HarnessDetail
  harnesses: Harness[]
  runs: RunRow[] | null
  tags: VersionTags
  onTags: (next: VersionTags) => void
  onBack: () => void
  onOpenRun: (runId: string) => void
  onOpenSpec: () => void
  /** Opens a fresh run on a version — which means the folder that holds it. */
  onStartRun: (harnessId: string) => void
  initialTab?: Tab
  /** Inside the chat workbench's contextual inspector, which owns the header. */
  embedded?: boolean
}) {
  const [tab, setTab] = useState<Tab>(initialTab)
  const [proposals, setProposals] = useState<Proposal[]>([])
  const [selected, setSelected] = useState<string | null>(null)

  useEffect(() => setTab(initialTab), [initialTab])

  useEffect(() => {
    let live = true
    api
      .proposals(harness.id)
      .then((rows) => live && setProposals(rows))
      .catch(() => live && setProposals([]))
    return () => {
      live = false
    }
  }, [harness.id])

  const branches = useMemo(
    () => harnesses.filter((item) => item.ok && item.name === harness.name),
    [harnesses, harness.name],
  )

  const graph = useMemo(
    () => buildVersionGraph(harness.name, branches, runs ?? [], proposals, harness.stats),
    [branches, harness.name, harness.stats, proposals, runs],
  )

  const open =
    graph.nodes.find((node) => node.hash === selected) ??
    graph.nodes.find((node) => node.hash === harness.version_hash) ??
    graph.nodes[0] ??
    null

  const setTag = useCallback(
    async (version: string, label: string) => {
      onTags(await api.setTag(harness.id, version, label))
    },
    [harness.id, onTags],
  )

  return (
    <>
      {!embedded && <header className="main-header">
        <button className="icon-btn" title="Back to the harness" onClick={onBack}>
          <i className="ph ph-arrow-left" />
        </button>
        <h1 style={{ fontSize: 15 }}>{harness.name}</h1>
        <span className="main-sub">versions</span>
      </header>}

      <nav className="tabbar">
        <button className="v-tab" data-on={tab === 'graph' ? '1' : '0'} onClick={() => setTab('graph')}>
          Graph
        </button>
        <button
          className="v-tab"
          data-on={tab === 'compare' ? '1' : '0'}
          onClick={() => setTab('compare')}
        >
          Compare
        </button>
        {!embedded && (
          <button className="v-tab" data-on="0" onClick={onOpenSpec}>
            Spec
          </button>
        )}
      </nav>

      {tab === 'compare' ? (
        <VersionCompare
          harness={harness}
          runs={runs ?? []}
          tags={tags}
          nodes={graph.nodes}
          left={open?.parent ?? open?.hash ?? ''}
          right={open?.hash ?? ''}
          onOpenRun={onOpenRun}
        />
      ) : runs === null ? (
        <div className="empty">Loading…</div>
      ) : graph.nodes.length === 0 ? (
        <div className="empty">
          <i className="ph ph-git-branch" style={{ fontSize: 26, display: 'block', marginBottom: 10 }} />
          No version on disk and none in the Hive — fix the spec, then run it.
        </div>
      ) : (
        <div className="versions-page">
          <div>
            <div className="graph-head">
              <div style={{ minWidth: 0 }}>
                <div className="graph-title-row">
                  <Label>Version graph</Label>
                  <span className="graph-chip">
                    <i className="ph ph-hexagon" style={{ color: 'var(--mut)' }} />
                    {harness.name}
                  </span>
                  <label className="graph-chip" title="Jump to a version in the graph">
                    <i className="ph ph-git-commit" style={{ color: 'var(--mut)' }} />
                    <select
                      className="mono"
                      value={open?.hash ?? ''}
                      onChange={(event) => setSelected(event.target.value)}
                    >
                      {graph.nodes.map((node) => (
                        <option key={node.hash} value={node.hash}>
                          {node.hash.slice(0, 8)} · {tags[node.hash] ?? node.kind}
                        </option>
                      ))}
                    </select>
                  </label>
                </div>
                <p className="graph-note">
                  Every version of {harness.name} that left evidence behind, including forks that
                  never rejoined the trunk.
                </p>
              </div>
              <div className="graph-legend">
                <span>
                  <span className="legend-dot" style={{ borderColor: 'var(--acc)', background: 'var(--acc)' }} />
                  evolved
                </span>
                <span>
                  <span className="legend-dot" style={{ borderColor: 'var(--evo)', background: 'var(--evo)' }} />
                  fork
                </span>
                <span>
                  <span className="legend-dot" style={{ borderColor: 'var(--dim)', background: 'var(--bg)' }} />
                  hand edit
                </span>
                <span>
                  <span className="legend-dot" style={{ borderColor: 'var(--mut)', background: 'var(--bg)' }} />
                  first version
                </span>
              </div>
            </div>

            <div className="graph-panel">
              {graph.nodes.map((node, index) => (
                <GraphRow
                  key={node.hash}
                  node={node}
                  index={index}
                  edges={graph.edges}
                  nodes={graph.nodes}
                  laneCount={graph.laneCount}
                  tag={tags[node.hash]}
                  selected={open?.hash === node.hash}
                  onSelect={() => setSelected(node.hash)}
                />
              ))}
            </div>
          </div>

          {open && (
            <LineagePanel
              node={open}
              graphNodes={graph.nodes}
              branchedHere={childrenOf(graph, open.hash)}
              ancestors={ancestryOf(graph, open.hash).slice(1)}
              tag={tags[open.hash]}
              onTag={setTag}
              onSelect={setSelected}
              onOpenRun={onOpenRun}
              onOpenSpec={onOpenSpec}
              onStartRun={onStartRun}
              onCompare={() => setTab('compare')}
            />
          )}
        </div>
      )}
    </>
  )
}

/* --------------------------------------------------------------- the graph */

function GraphRow({
  node,
  index,
  edges,
  nodes,
  laneCount,
  tag,
  selected,
  onSelect,
}: {
  node: VersionNode
  index: number
  edges: { child: number; parent: number }[]
  nodes: VersionNode[]
  laneCount: number
  tag?: string
  selected: boolean
  onSelect: () => void
}) {
  const colour = KIND_COLOR[node.kind] ?? 'var(--dim)'
  const passed = node.runs.filter((run) => run.status === 'success').length
  const laneX = (lane: number) => LANE_ORIGIN + lane * LANE_STEP

  // Each row draws the slice of every edge that crosses it. Drawing edges as
  // per-row segments rather than one absolutely-positioned overlay is what
  // keeps the graph aligned when a row's height is decided by its content.
  const segments: React.CSSProperties[] = []
  for (const edge of edges) {
    if (index < edge.child || index > edge.parent) continue
    const childLane = nodes[edge.child].lane
    const parentLane = nodes[edge.parent].lane
    const colourOf =
      childLane === parentLane
        ? childLane === 0
          ? 'var(--lane-trunk)'
          : 'var(--lane-fork)'
        : 'var(--lane-fork)'
    if (childLane === parentLane) {
      const top = index === edge.child ? ROW_CENTRE : 0
      const height =
        index === edge.child
          ? ROW_HEIGHT - ROW_CENTRE
          : index === edge.parent
            ? ROW_CENTRE
            : ROW_HEIGHT
      segments.push({
        position: 'absolute',
        left: laneX(parentLane) - 0.75,
        top,
        width: 1.5,
        height,
        background: colourOf,
      })
      continue
    }
    if (index === edge.child) {
      // The elbow: down from the fork's lane, then left into the parent's.
      segments.push({
        position: 'absolute',
        left: laneX(parentLane),
        top: ROW_CENTRE,
        width: laneX(childLane) - laneX(parentLane),
        height: ROW_HEIGHT - ROW_CENTRE,
        borderRight: `1.5px solid ${colourOf}`,
        borderBottom: `1.5px solid ${colourOf}`,
        borderBottomRightRadius: 10,
      })
    } else {
      segments.push({
        position: 'absolute',
        left: laneX(parentLane) - 0.75,
        top: 0,
        width: 1.5,
        height: index === edge.parent ? ROW_CENTRE : ROW_HEIGHT,
        background: colourOf,
      })
    }
  }

  return (
    <button
      className="graph-row"
      data-on={selected ? '1' : '0'}
      style={{ gridTemplateColumns: `${laneX(Math.max(1, laneCount - 1)) + 34}px minmax(0,1fr)` }}
      onClick={onSelect}
    >
      <span className="graph-track" style={{ height: ROW_HEIGHT }}>
        {segments.map((style, key) => (
          <span key={key} style={style} />
        ))}
        <span
          className="graph-dot"
          style={{
            left: laneX(node.lane) - 5,
            top: ROW_CENTRE - 5,
            borderColor: colour,
            background: node.kind === 'fork' || tag ? colour : 'var(--bg)',
          }}
        />
      </span>

      <span className="graph-body">
        <span className="graph-line">
          <strong className="mono">{node.hash.slice(0, 8)}</strong>
          <span className="kind-chip" style={{ borderColor: colour, color: colour }}>
            {node.kind}
          </span>
          {tag && <span className="rail-tag">{tag}</span>}
          {node.kind === 'fork' && node.folder && (
            <span className="mono graph-folder">{node.folder}</span>
          )}
          <span className="mono graph-when">{node.createdAt ? when(node.createdAt) : '—'}</span>
        </span>
        <span className="graph-title ellipsis">{node.title}</span>
        <span className="graph-meta mono">
          <span>
            <i className="ph ph-list-magnifying-glass" />
            {node.runs.length === 1 ? '1 run' : `${node.runs.length} runs`}
          </span>
          {node.runs.slice(0, 2).map((run) => (
            <span key={run.run_id} className="graph-run ellipsis">
              {run.task || run.run_id}
            </span>
          ))}
          <span style={{ marginLeft: 'auto', flex: 'none' }}>
            {node.runs.length === 0 ? 'never run' : `${passed}/${node.runs.length} passed`}
          </span>
          <span className="graph-dots">
            {node.runs.slice(0, 5).map((run) => (
              <span
                key={run.run_id}
                title={`${run.run_id} · ${run.status}`}
                style={{ background: statusColor(run.status) }}
              />
            ))}
            {node.runs.length > 5 && <span className="graph-more">+{node.runs.length - 5}</span>}
          </span>
        </span>
      </span>
    </button>
  )
}

/* ----------------------------------------------------------- the inspector */

function LineagePanel({
  node,
  graphNodes,
  branchedHere,
  ancestors,
  tag,
  onTag,
  onSelect,
  onOpenRun,
  onOpenSpec,
  onStartRun,
  onCompare,
}: {
  node: VersionNode
  graphNodes: VersionNode[]
  branchedHere: VersionNode[]
  ancestors: VersionNode[]
  tag?: string
  onTag: (version: string, label: string) => Promise<void>
  onSelect: (hash: string) => void
  onOpenRun: (runId: string) => void
  onOpenSpec: () => void
  onStartRun: (harnessId: string) => void
  onCompare: () => void
}) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(tag ?? '')
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setEditing(false)
    setDraft(tag ?? '')
    setError(null)
  }, [node.hash, tag])

  const parent = graphNodes.find((item) => item.hash === node.parent) ?? null
  const colour = KIND_COLOR[node.kind] ?? 'var(--dim)'

  const commit = async (value: string) => {
    try {
      await onTag(node.hash, value)
      setEditing(false)
      setError(null)
    } catch (exc) {
      setError(String(exc))
    }
  }

  return (
    <aside className="lineage-panel">
      <div className="lineage-head">
        <strong className="mono">{node.hash.slice(0, 8)}</strong>
        <span className="kind-chip" style={{ borderColor: colour, color: colour }}>
          {node.kind}
        </span>
      </div>

      <div className="lineage-tag">
        <i className="ph ph-tag" />
        {editing ? (
          <div className="lineage-tag-edit">
            <input
              autoFocus
              value={draft}
              placeholder="e.g. baseline"
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter') void commit(draft.trim())
                if (event.key === 'Escape') setEditing(false)
              }}
            />
            <button className="v-btn v-btn-primary v-btn-sm" onClick={() => void commit(draft.trim())}>
              Save
            </button>
            <button className="icon-bare" title="Cancel" onClick={() => setEditing(false)}>
              <i className="ph ph-x" />
            </button>
          </div>
        ) : (
          <div className="lineage-tag-read">
            <span className="rail-tag" data-empty={tag ? '0' : '1'}>
              {tag ?? 'untagged'}
            </span>
            <button
              className="v-btn v-btn-ghost v-btn-sm"
              style={{ marginLeft: 'auto' }}
              onClick={() => setEditing(true)}
            >
              <i className="ph ph-pencil-simple" />
              {tag ? 'Rename' : 'Add tag'}
            </button>
            {tag && (
              <button className="icon-bare danger" title="Remove this tag" onClick={() => void commit('')}>
                <i className="ph ph-trash" />
              </button>
            )}
          </div>
        )}
      </div>
      {error && (
        <div style={{ marginTop: 9 }}>
          <Notice icon="ph-warning" tone="err" title="Could not save the tag" body={error} />
        </div>
      )}

      <p className="lineage-note">{node.note}</p>

      <dl className="event-fields" style={{ marginTop: 14 }}>
        <div>
          <dt>created</dt>
          <dd className="mono">{node.createdAt ? when(node.createdAt) : 'no evidence'}</dd>
        </div>
        <div>
          <dt>parent</dt>
          <dd className="mono">
            {parent ? parent.hash.slice(0, 12) : node.kind === 'initial' ? 'none — first version' : 'not recorded'}
          </dd>
        </div>
        <div>
          <dt>folder</dt>
          <dd className="mono">{node.folder || 'no folder holds it any more'}</dd>
        </div>
        {node.folders.length > 1 && (
          <div>
            <dt>also in</dt>
            <dd className="mono">
              {node.folders.filter((item) => item !== node.folder).join(', ')}
            </dd>
          </div>
        )}
        <div>
          <dt>model</dt>
          <dd className="mono">{node.modelPath || 'not recorded'}</dd>
        </div>
      </dl>

      <div className="lineage-section">
        <Label>Changed from parent · {node.changes.length}</Label>
        {node.changes.length === 0 ? (
          <p className="lineage-empty">
            {node.kind === 'initial'
              ? 'Nothing to compare — this is where the harness starts.'
              : 'No record names what changed. The Compare tab reads the evidence instead.'}
          </p>
        ) : (
          <div className="lineage-changes">
            {node.changes.map((change) => (
              <div key={change.path}>
                <code>{change.path}</code>
                <div className="mono lineage-change">
                  {change.from !== null && <span className="was">{change.from}</span>}
                  {change.from !== null && <i className="ph ph-arrow-right" />}
                  <span className="now">{change.to}</span>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {branchedHere.length > 0 && (
        <div className="lineage-section">
          <Label>Branched off here · {branchedHere.length}</Label>
          <div className="lineage-list">
            {branchedHere.map((child) => (
              <button key={child.hash} title={child.title} onClick={() => onSelect(child.hash)}>
                <i className="ph ph-git-fork" style={{ color: 'var(--evo)' }} />
                <span className="mono">
                  {child.hash.slice(0, 8)} · {child.kind}
                </span>
                <i className="ph ph-arrow-right" style={{ marginLeft: 'auto' }} />
              </button>
            ))}
          </div>
        </div>
      )}

      {ancestors.length > 0 && (
        <div className="lineage-section">
          <Label>Descends from · {ancestors.length}</Label>
          <div className="lineage-list">
            {ancestors.map((item) => (
              <button key={item.hash} title={item.title} onClick={() => onSelect(item.hash)}>
                <i className="ph ph-git-commit" style={{ color: 'var(--mut)' }} />
                <span className="mono">
                  {item.hash.slice(0, 8)} · {item.kind}
                </span>
                <i className="ph ph-arrow-right" style={{ marginLeft: 'auto' }} />
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="lineage-section">
        <Label>Runs on this version · {node.runs.length}</Label>
        {node.runs.length === 0 ? (
          <p className="lineage-empty">Never run.</p>
        ) : (
          <div className="lineage-list">
            {node.runs.slice(0, 12).map((run) => (
              <button key={run.run_id} onClick={() => onOpenRun(run.run_id)}>
                <span className="dot" style={{ background: statusColor(run.status) }} />
                <span className="mono ellipsis">{run.run_id.replace(/^run_/, '')}</span>
                <span className="mono lineage-when">
                  ${run.cost_usd.toFixed(4)} · {run.turns}t
                </span>
              </button>
            ))}
          </div>
        )}
      </div>

      <button
        className="v-btn v-btn-primary"
        style={{ width: '100%', marginTop: 18 }}
        disabled={!node.harnessId}
        title={
          node.harnessId
            ? 'Opens a new run on this version'
            : 'No folder holds this version any more, so it cannot be run'
        }
        onClick={() => node.harnessId && onStartRun(node.harnessId)}
      >
        <i className="ph ph-play" />
        Start a run on this version
      </button>

      <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
        <button className="v-btn v-btn-ghost v-btn-sm" onClick={onCompare}>
          <i className="ph ph-git-diff" />
          Compare with parent
        </button>
        <button className="v-btn v-btn-ghost v-btn-sm" onClick={onOpenSpec}>
          <i className="ph ph-file-code" />
          Spec
        </button>
      </div>
    </aside>
  )
}

/* ------------------------------------------------------------- comparison */

/** Did it help? Two versions, their deltas, and which failures moved. */
function VersionCompare({
  harness,
  runs,
  tags,
  nodes,
  left: initialLeft,
  right: initialRight,
  onOpenRun,
}: {
  harness: HarnessDetail
  runs: RunRow[]
  tags: VersionTags
  nodes: VersionNode[]
  left: string
  right: string
  onOpenRun: (runId: string) => void
}) {
  const [left, setLeft] = useState(initialLeft)
  const [right, setRight] = useState(initialRight)
  const [result, setResult] = useState<Comparison | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    setLeft(initialLeft || nodes[1]?.hash || '')
    setRight(initialRight || nodes[0]?.hash || '')
    setResult(null)
  }, [initialLeft, initialRight, nodes])

  const compare = async () => {
    setBusy(true)
    setError(null)
    try {
      setResult(await api.compare(harness.id, left, right))
    } catch (exc) {
      setError(String(exc))
    } finally {
      setBusy(false)
    }
  }

  if (nodes.length < 2) {
    return (
      <div className="pane">
        <Label>Compare versions</Label>
        <p className="evolve-note">
          Only one version exists. Apply a change or fork the harness and run it again — the
          comparison is how you find out whether it helped.
        </p>
      </div>
    )
  }

  const option = (node: VersionNode) => (
    <option key={node.hash} value={node.hash}>
      {node.hash.slice(0, 8)} · {tags[node.hash] ?? node.kind} · {node.runs.length} runs
    </option>
  )

  return (
    <div className="pane">
      <div style={{ maxWidth: 900 }}>
        <Label>Compare versions</Label>
        <p className="evolve-lede">
          Two versions of the same harness, side by side on the evidence they actually recorded.
        </p>

        <div className="compare-pickers">
          <select className="v-input mono" value={left} onChange={(event) => setLeft(event.target.value)}>
            {nodes.map(option)}
          </select>
          <i className="ph ph-arrow-right" />
          <select className="v-input mono" value={right} onChange={(event) => setRight(event.target.value)}>
            {nodes.map(option)}
          </select>
          <button
            className="v-btn v-btn-ghost v-btn-sm"
            onClick={() => void compare()}
            disabled={!left || !right || left === right || busy}
          >
            {busy ? (
              <i className="ph ph-circle-notch" style={{ animation: 'spin 1s linear infinite' }} />
            ) : null}
            Compare
          </button>
        </div>

        {error && (
          <div style={{ marginTop: 12 }}>
            <Notice icon="ph-warning" tone="err" title="Could not compare" body={error} />
          </div>
        )}

        {result && (
          <div className="compare-result">
            {result.underpowered && (
              <Notice
                icon="ph-warning"
                tone="warn"
                title="Not enough runs to call it"
                body="One of these versions has fewer than five runs. The deltas below are description, not evidence."
              />
            )}
            <StatRow>
              <Stat
                label="success"
                value={`${signed(result.delta.success_rate * 100)} pts`}
                color={result.delta.success_rate >= 0 ? 'var(--ok)' : 'var(--err)'}
              />
              <Stat label="cost / run" value={signed(result.delta.avg_cost_usd, 4)} />
              <Stat label="turns" value={signed(result.delta.avg_turns, 1)} />
              <Stat label="runs" value={`${result.left.runs} → ${result.right.runs}`} />
            </StatRow>

            <div className="best-pair">
              {[left, right].map((version, index) => {
                const run = bestRun(runs, version)
                return (
                  <div className="best-run" key={index}>
                    <Label>Best run · {version.slice(0, 12)}</Label>
                    {run ? (
                      <button className="best-open" onClick={() => onOpenRun(run.run_id)}>
                        <StatusPill status={run.status} />
                        <span className="mono">{run.run_id.replace(/^run_/, '')}</span>
                        <span className="mono best-meta">
                          ${run.cost_usd.toFixed(4)} · {run.turns} turns
                        </span>
                        <i className="ph ph-arrow-right" />
                      </button>
                    ) : (
                      <p className="evolve-note">No successful run on this version.</p>
                    )}
                  </div>
                )
              })}
            </div>

            <div className="compare-failures">
              <section>
                <Label>Stopped appearing · {result.fixed_failures.length}</Label>
                {result.fixed_failures.length === 0 ? (
                  <p className="evolve-note">No failure signature disappeared.</p>
                ) : (
                  result.fixed_failures.map((item) => (
                    <div className="evidence-line" key={item}>
                      <i className="ph ph-check-circle" style={{ color: 'var(--ok)' }} />
                      <span>{item}</span>
                    </div>
                  ))
                )}
              </section>
              <section>
                <Label>New · {result.new_failures.length}</Label>
                {result.new_failures.length === 0 ? (
                  <p className="evolve-note">No new failure signature appeared.</p>
                ) : (
                  result.new_failures.map((item) => (
                    <div className="evidence-line" key={item}>
                      <i className="ph ph-x-circle" style={{ color: 'var(--err)' }} />
                      <span>{item}</span>
                    </div>
                  ))
                )}
              </section>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

function signed(value: number, digits = 1): string {
  const text = value.toFixed(digits)
  return value > 0 ? `+${text}` : text
}
