import { useEffect, useMemo, useState } from 'react'
import { groupHarnesses, groupLabel } from '../harnesses'
import { runLabel } from '../runs'
import type { Harness, RunRow, VersionTags } from '../types'
import { statusColor, when } from './common'

/**
 * The rail: harness → version → run.
 *
 * Three levels, because those are the three things that are actually distinct.
 * A **harness** is one row, not one folder: its forks live inside it, at
 * `.hiveloom/forks/<name>`, and belong under the row they are experiments on
 * rather than beside it. That grouping follows the folder — `root_path` from
 * the server — not the name: a fork starts out sharing its parent's name but
 * renaming it is an ordinary spec edit, and two unrelated harnesses that
 * happen to share a name are still two harnesses. A **version** is what a run
 * was actually recorded against — the thing you compare, tag, and evolve — so
 * it sits above its runs rather than being a chip inside them. Workbench, CLI,
 * and deployed runs all have the same shape and appear in the same list.
 */
export function HarnessList({
  items,
  selected,
  runs,
  tags,
  selectedRun,
  liveRunId,
  onSelect,
  onSelectRun,
  onOpenGraph,
  onOpenCompare,
  onOpenSpec,
}: {
  items: Harness[] | null
  selected: string | null
  runs: RunRow[] | null
  tags: VersionTags
  selectedRun: string | null
  liveRunId: string | null
  onSelect: (id: string) => void
  onSelectRun: (runId: string) => void
  onOpenGraph: (id: string) => void
  onOpenCompare: (id: string) => void
  onOpenSpec: (id: string) => void
}) {
  const [menu, setMenu] = useState<string | null>(null)

  if (items === null) {
    return (
      <div className="empty" style={{ padding: '32px 12px' }}>
        <i className="ph ph-circle-notch" style={{ animation: 'spin 1s linear infinite' }} />
      </div>
    )
  }
  if (items.length === 0) {
    return (
      <div className="empty" style={{ padding: '32px 12px', fontSize: 13 }}>
        No harnesses yet.
      </div>
    )
  }

  return (
    <>
      <div className="v-label rail-heading">Harnesses</div>
      {groupHarnesses(items).map((group) => {
        const { key, trunk, forks } = group
        const branches = [trunk, ...forks]
        const open = branches.some((branch) => branch.id === selected)
        const active = branches.find((branch) => branch.id === selected) ?? trunk
        const name = groupLabel(group)
        return (
          <div key={key} className="rail-group">
            <div className="side-item" data-on={open ? '1' : '0'}>
              <button className="side-item-open" onClick={() => onSelect(trunk.id)}>
                <div className="side-item-name">
                  <span className="dot" style={{ background: dotColor(active) }} />
                  <span className="ellipsis">{name}</span>
                </div>
                <div className="mono side-item-sub">
                  {!active.ok
                    ? 'does not load'
                    : active.stats && active.stats.total_runs > 0
                      ? `${Math.round(active.stats.success_rate * 100)}% · ${active.stats.total_runs} runs`
                      : 'never run'}
                  {active.explicit && ' · --dir'}
                  {/* Running a fork instead of the trunk is worth saying here. */}
                  {active.fork && ` · fork @ turn ${active.fork.at_turn}`}
                </div>
              </button>

              <button
                className="rail-menu-btn"
                data-on={menu === key ? '1' : '0'}
                title="More for this harness"
                onClick={() => setMenu((current) => (current === key ? null : key))}
              >
                <i className="ph ph-dots-three" />
              </button>

              {menu === key && (
                <>
                  <div className="rail-menu-scrim" onClick={() => setMenu(null)} />
                  <div className="rail-menu">
                    <button
                      onClick={() => {
                        setMenu(null)
                        onOpenGraph(trunk.id)
                      }}
                    >
                      <i className="ph ph-git-branch" style={{ color: 'var(--evo)' }} />
                      Version graph
                    </button>
                    <button
                      onClick={() => {
                        setMenu(null)
                        onOpenCompare(trunk.id)
                      }}
                    >
                      <i className="ph ph-git-diff" style={{ color: 'var(--acc)' }} />
                      Compare versions
                    </button>
                    <button
                      onClick={() => {
                        setMenu(null)
                        onOpenSpec(trunk.id)
                      }}
                    >
                      <i className="ph ph-file-code" />
                      Spec
                    </button>
                  </div>
                </>
              )}
            </div>

            {open && (
              <>
                {/* Forks first: they are the experiments *on* the versions
                    below, and one of them may be what is selected. */}
                {forks.map((fork) => (
                  <button
                    key={fork.id}
                    className="rail-fork"
                    data-on={fork.id === selected ? '1' : '0'}
                    title={
                      fork.fork
                        ? `forked from ${fork.fork.parent_run_id} at turn ${fork.fork.at_turn}` +
                          ` — ${fork.path}`
                        : fork.path
                    }
                    onClick={() => onSelect(fork.id)}
                  >
                    <i className="ph ph-git-fork" />
                    <span className="ellipsis">{fork.folder}</span>
                    {fork.fork && <span className="rail-run-when">turn {fork.fork.at_turn}</span>}
                  </button>
                ))}
                <VersionRail
                  harness={active}
                  runs={runs}
                  tags={tags}
                  selectedRun={selectedRun}
                  liveRunId={liveRunId}
                  onSelectRun={onSelectRun}
                />
              </>
            )}
          </div>
        )
      })}
    </>
  )
}

/**
 * The versions of one harness, newest first, each holding its runs.
 *
 * The version list is the Hive's — every version that has runs — plus the one
 * on disk right now, which may have none yet. That last one matters: a harness
 * you just edited has a version no run has ever used, and it is the version
 * your next message will be pinned to.
 */
function VersionRail({
  harness,
  runs,
  tags,
  selectedRun,
  liveRunId,
  onSelectRun,
}: {
  harness: Harness
  runs: RunRow[] | null
  tags: VersionTags
  selectedRun: string | null
  liveRunId: string | null
  onSelectRun: (runId: string) => void
}) {
  const [open, setOpen] = useState<string | null>(harness.version_hash ?? null)

  const versions = useMemo(() => {
    const counts = new Map<string, { runs: number; successes: number }>()
    for (const entry of harness.stats?.versions ?? []) {
      counts.set(entry.version, {
        runs: entry.runs,
        successes: Math.round(entry.success_rate * entry.runs),
      })
    }
    if (harness.version_hash && !counts.has(harness.version_hash)) {
      counts.set(harness.version_hash, { runs: 0, successes: 0 })
    }
    const rows = [...counts.entries()].map(([hash, count]) => ({ hash, ...count }))
    // The Hive already orders its versions newest first; the on-disk one, when
    // it has no runs, is newer than all of them by definition.
    rows.sort((a, b) => {
      if (a.hash === harness.version_hash) return -1
      if (b.hash === harness.version_hash) return 1
      return 0
    })
    return rows
  }, [harness])

  // A run opened from anywhere reveals the version it belongs to.
  useEffect(() => {
    if (!selectedRun) return
    const owner = (runs ?? []).find((run) => run.run_id === selectedRun)
    if (owner) setOpen(owner.harness_version_hash)
  }, [runs, selectedRun])

  useEffect(() => {
    setOpen(harness.version_hash ?? null)
  }, [harness.id, harness.version_hash])

  if (runs === null) return <div className="rail-note">loading…</div>
  if (versions.length === 0) {
    return <div className="rail-note">No version on disk yet — fix the spec first.</div>
  }

  return (
    <div className="rail-versions">
      {versions.map((version, index) => {
        const isOpen = open === version.hash
        const own = (runs ?? [])
          .filter((run) => run.harness_version_hash === version.hash)
          .sort((a, b) => Date.parse(b.started_at ?? '') - Date.parse(a.started_at ?? ''))
        const rate = version.runs > 0 ? Math.round((version.successes / version.runs) * 100) : null
        const label = tags[version.hash]
        return (
          <div key={version.hash}>
            <button
              className="rail-version"
              data-on={isOpen ? '1' : '0'}
              title={`${version.hash}${label ? ` · ${label}` : ''}`}
              onClick={() => setOpen((current) => (current === version.hash ? null : version.hash))}
            >
              <i className={`ph ${isOpen ? 'ph-caret-down' : 'ph-caret-right'}`} />
              <span className="ellipsis mono">{version.hash.slice(0, 8)}</span>
              {label ? (
                <span className="rail-tag">{label}</span>
              ) : (
                index === 0 && version.hash === harness.version_hash && (
                  <span className="rail-tag">latest</span>
                )
              )}
              <span className="rail-run-when">
                {rate === null
                  ? 'no runs'
                  : `${rate}% · ${version.runs} ${version.runs === 1 ? 'run' : 'runs'}`}
              </span>
            </button>

            {isOpen && (
              <div className="rail-version-body">
                {own.map((run) => (
                  <button
                    key={run.run_id}
                    className="rail-run"
                    data-on={selectedRun === run.run_id ? '1' : '0'}
                    title={`${run.status} · ${run.turns} turns · $${run.cost_usd.toFixed(4)}`}
                    onClick={() => onSelectRun(run.run_id)}
                  >
                    <span className="dot" style={{ background: statusColor(run.status) }} />
                    <span className="ellipsis">{runLabel(run)}</span>
                    <span className="rail-run-when">
                      {run.run_id === liveRunId ? 'live' : when(run.started_at).split(',')[0]}
                    </span>
                  </button>
                ))}

                {own.length === 0 && <div className="rail-note">No runs on this version.</div>}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

function dotColor(item: Harness): string {
  if (!item.ok) return statusColor('error')
  if (!item.stats || item.stats.total_runs === 0) return 'var(--mut)'
  if (item.stats.success_rate >= 0.8) return 'var(--ok)'
  if (item.stats.success_rate < 0.5) return 'var(--err)'
  return 'var(--warn)'
}

