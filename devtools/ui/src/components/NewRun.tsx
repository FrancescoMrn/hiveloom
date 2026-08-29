/**
 * The empty workspace: a composer in the middle of the pane, not pinned under an
 * empty thread.
 *
 * Two decisions belong here, at the point of action rather than in the rail:
 * which harness, and which version of it. The version seat lists what can
 * actually be run — the harness's folders, each labelled by the version it is
 * at and the tag someone gave it — because a fork of an unedited harness *is*
 * the same version, and offering "trunk / turn0 / turn2" would be offering
 * three names for one thing.
 *
 * Versions with runs but no folder are history: they are counted here, and
 * compared in the version graph, but they cannot be run, so they are not
 * offered.
 *
 * On a fork there is a third decision, and it is a mode rather than an action:
 * a fork exists to be resumed, so resuming is what it offers by default, and
 * starting a fresh input is the deliberate other choice. It sits with the
 * other two seats because it is the same kind of decision — what this run *is*
 * — and a lone button above the composer read as an afterthought beside an
 * input it makes no use of.
 */
import { useEffect, useState } from 'react'
import logo from '../../../../docs/assets/logo.png'
import { Composer } from './Composer'
import { specSelector } from '../models'
import { taskGuideFor } from '../taskGuide'
import type { DeliverMode } from '../prefs'
import type { Harness, HarnessDetail, Provider, VersionTags } from '../types'
import type { RunWorkspace } from '../useRun'

export function NewRun({
  harness,
  harnesses,
  workspace,
  providers,
  seatModels,
  tags,
  deliver,
  onDeliver,
  disabled,
  focusToken,
  onSelect,
  onOpenGraph,
}: {
  harness: HarnessDetail
  harnesses: Harness[]
  workspace: RunWorkspace
  providers: Provider[] | null
  /** The seat's shortlist for this harness; empty means every usable model. */
  seatModels?: string[]
  tags: VersionTags
  deliver: DeliverMode
  onDeliver: (mode: DeliverMode) => void
  disabled: boolean
  focusToken: number
  onSelect: (id: string) => void
  onOpenGraph: () => void
}) {
  // Default on: a fork's whole reason to exist is the prefix it carries.
  // Reset per folder, so turning it off on one fork does not silently follow
  // you to the next one.
  const [resume, setResume] = useState(true)
  useEffect(() => setResume(true), [harness.id])
  const fork = harness.fork ?? null
  const guide = taskGuideFor(harness)

  const runsFor = (version?: string) =>
    harness.stats?.versions.find((entry) => entry.version === version)?.runs ?? 0

  // One option per *version*, not per folder. Two folders at one version are
  // one version — an unedited fork is exactly that — and offering both would
  // be offering two names for the same thing. The trunk wins the tie, since
  // running the fork's folder would resume its context instead.
  const byVersion = new Map<string, Harness>()
  for (const item of harnesses) {
    if (item.name !== harness.name || !item.ok) continue
    const key = item.version_hash ?? item.id
    const held = byVersion.get(key)
    if (!held || (held.fork && !item.fork)) byVersion.set(key, item)
  }
  const folders = [...byVersion.values()].sort((a, b) => {
    // The version you are on first, then by how much evidence each has.
    if (a.version_hash === harness.version_hash) return -1
    if (b.version_hash === harness.version_hash) return 1
    return runsFor(b.version_hash) - runsFor(a.version_hash)
  })

  const historical = (harness.stats?.versions ?? []).filter(
    (entry) => !folders.some((folder) => folder.version_hash === entry.version),
  )

  return (
    <div className="new-run">
      <div className="new-run-inner">
        <img className="hero-mark" src={logo} alt="" width={40} height={40} />
        <div className="new-run-heading">
          <div className="v-label">Run this harness</div>
          <h2 className="new-run-title">{harness.name}</h2>
          <p>{harness.description}</p>
        </div>

        <div className="task-contract">
          <div className="task-contract-icon"><i className="ph ph-arrow-line-right" /></div>
          <div>
            <div className="v-label">Input · {guide.inputLabel}</div>
            <p>{guide.inputHelp}</p>
          </div>
          <div className="task-contract-arrow"><i className="ph ph-arrow-right" /></div>
          <div>
            <div className="v-label">Harness result</div>
            <p>One recorded run with output, verifier feedback, cost, and a full trajectory.</p>
          </div>
        </div>

        <div className="hero-seats">
          <label className="hero-seat" title="Which harness this workspace runs">
            <i className="ph ph-hexagon" />
            <select value={harness.id} onChange={(event) => onSelect(event.target.value)}>
              {pickable(harnesses, harness.id).map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                  {item.fork ? ` · fork @ turn ${item.fork.at_turn}` : ''}
                </option>
              ))}
            </select>
          </label>

          <label className="hero-seat" title="The version this workspace is pinned to">
            <i className="ph ph-git-commit" />
            <select
              className="mono"
              value={harness.id}
              onChange={(event) => onSelect(event.target.value)}
            >
              {folders.map((item) => {
                const version = item.version_hash ?? ''
                return (
                  <option key={item.id} value={item.id}>
                    {(version || 'unversioned').slice(0, 8)} ·{' '}
                    {tags[version] ??
                      (version === harness.version_hash
                        ? 'latest'
                        : plural(runsFor(version), 'run'))}
                  </option>
                )
              })}
            </select>
          </label>

          {fork && (
            <div
              className="mode-toggle"
              title="A fork re-enters its parent's conversation; a new input starts a fresh one"
            >
              <button
                data-on={resume ? '1' : '0'}
                onClick={() => setResume(true)}
                title={`Replay the parent's prefix and continue from turn ${fork.at_turn}`}
              >
                <i className="ph ph-play" />
                Resume @ turn {fork.at_turn}
              </button>
              <button
                data-on={resume ? '0' : '1'}
                onClick={() => setResume(false)}
                title="Ignore the recorded prefix and start this folder on a fresh task"
              >
                <i className="ph ph-pencil-simple-line" />
                New input
              </button>
            </div>
          )}
        </div>

        {fork && resume ? (
          <div className="resume-card">
            <p>
              Resuming replays the parent run's conversation up to turn {fork.at_turn} and
              continues from there against this folder's harness. The prefix is seeded, not
              re-executed, and no new task statement is added — so there is nothing to type.
            </p>
            <button
              className="v-btn v-btn-primary"
              disabled={disabled || workspace.busy}
              onClick={() => void workspace.resumeFork()}
            >
              <i className="ph ph-play" />
              Resume from turn {fork.at_turn}
            </button>
          </div>
        ) : (
          <Composer
            harnessId={harness.id}
            harnessName={harness.name}
            ownModel={specSelector(harness)}
            seatModels={seatModels}
            workspace={workspace}
            providers={providers}
            deliver={deliver}
            onDeliver={onDeliver}
            disabled={disabled}
            focusToken={focusToken}
            variant="hero"
            taskGuide={guide}
          />
        )}

        {!fork || !resume ? (
          <>
            <div className="experiment-guide" aria-label="Experiment workflow">
              <span><b>1</b> Run a representative input</span>
              <i className="ph ph-arrow-right" />
              <span><b>2</b> Inspect verifier evidence</span>
              <i className="ph ph-arrow-right" />
              <span><b>3</b> Improve from real failures</span>
            </div>
            <p className="experiment-tip">
              <i className="ph ph-flask" /> The model picker changes only this workspace. Keep the
              input fixed and switch models to run a clean executor experiment.
            </p>
          </>
        ) : null}

        {historical.length > 0 && (
          <p className="new-run-note">
            {plural(historical.length, 'earlier version')} with runs{' '}
            {historical.length === 1 ? 'is' : 'are'} history — no folder holds{' '}
            {historical.length === 1 ? 'it' : 'them'} any more, so{' '}
            {historical.length === 1 ? 'it' : 'they'} can be read in the{' '}
            <button className="link-btn" onClick={onOpenGraph}>
              version graph
            </button>{' '}
            but not run.
          </p>
        )}
      </div>
    </div>
  )
}

/**
 * One entry per harness, not per folder: the forks of a harness are versions of
 * it, and a picker that lists three rows called the same thing is the noise
 * this screen exists to avoid. A fork you are already inside still appears —
 * otherwise the picker could not show where you are.
 */
function pickable(harnesses: Harness[], current: string): Harness[] {
  const seen = new Set<string>()
  const rows: Harness[] = []
  for (const item of harnesses) {
    const trunk = !item.fork
    if (item.id === current || (trunk && !seen.has(item.name))) {
      if (trunk) seen.add(item.name)
      rows.push(item)
    }
  }
  return rows
}

function plural(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? '' : 's'}`
}
