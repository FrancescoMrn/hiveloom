/**
 * Branch the run at one of a run's model calls.
 *
 * Two different things can be called "forking a run", and this is the cheaper
 * one. A *harness* fork copies the folder and gives the branch a version of its
 * own — right when you want to change the harness. A *run* branch changes
 * nothing on disk: it opens a new conversation carrying the history the run had
 * at that boundary, on the same harness at the same version, so the only
 * variable that moved is the one you chose to move. Both are offered; this is
 * the one you want when the question is "what would a stronger model have done
 * from here".
 *
 * Only model calls are boundaries. A tool result is mid-turn, and re-entering
 * there would mean inventing the request that followed it.
 *
 * The history comes from `materialize`, the same fold `hiveloom trace
 * --materialize` and `hiveloom fork` use — so a legacy journal that cannot
 * answer says so rather than being replayed from a guess.
 */
import { useEffect, useState } from 'react'
import { api } from '../api'
import { modelOptions, overrideFor } from '../models'
import type { ForkPoint, MaterializedContext, Provider } from '../types'
import { Label, Notice } from './common'

export function BranchDialog({
  runId,
  point,
  version,
  currentModel,
  ownModel,
  seatModels = [],
  providers,
  onClose,
  onBranch,
}: {
  runId: string
  point: ForkPoint
  /** The harness version the parent run recorded; the branch keeps it. */
  version: string
  currentModel: string
  /** `provider/id` from the spec: the model a run uses with no override. */
  ownModel: string
  /** The seat's shortlist for this harness; empty means every usable model. */
  seatModels?: string[]
  providers: Provider[] | null
  onClose: () => void
  onBranch: (messages: { role: string; content: string }[], model: string) => void
}) {
  const [context, setContext] = useState<MaterializedContext | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [model, setModel] = useState(currentModel)

  useEffect(() => {
    let live = true
    api
      .context(runId, point.seq)
      .then((result) => live && setContext(result))
      .catch((exc) => live && setError(String(exc)))
    return () => {
      live = false
    }
  }, [point.seq, runId])

  const messages = context?.available ? flatten(context.request.messages) : []

  return (
    <div className="scrim" onClick={onClose}>
      <div className="v-panel modal rise" onClick={(event) => event.stopPropagation()}>
        <h2 style={{ fontSize: 17 }}>Branch the run at turn {point.turn}</h2>
        <p style={{ color: 'var(--dim)', fontSize: 13, marginTop: 6 }}>
          Takes this run's history up to seq {point.seq} and continues it in a new run on the
          same harness version. Nothing about the harness changes, and the parent run is left
          exactly as recorded.
        </p>

        {error && (
          <div style={{ marginTop: 18 }}>
            <Notice icon="ph-warning-octagon" tone="err" title="Could not read the boundary" body={error} />
          </div>
        )}

        {context && !context.available && (
          <div style={{ marginTop: 18 }}>
            <Notice
              icon="ph-clock-counter-clockwise"
              tone="warn"
              title="This journal cannot be replayed"
              body="It predates progressive context events, so the request at this boundary was never recorded. Re-run the harness on the current journal format, or fork the harness folder instead."
            />
          </div>
        )}

        <div style={{ marginTop: 20, display: 'grid', gap: 14 }}>
          <div className="branch-note">
            <i className="ph ph-clock-counter-clockwise" />
            <span>
              {context === null
                ? 'Folding the journal…'
                : `Replays ${point.turn} turn${point.turn === 1 ? '' : 's'} · ${messages.length} message${
                    messages.length === 1 ? '' : 's'
                  } · version ${version.slice(0, 8)} stays pinned`}
            </span>
          </div>

          <div>
            <Label>Model for the new run</Label>
            <select
              className="v-input mono"
              value={model || ownModel}
              style={{ marginTop: 6, fontSize: 13 }}
              onChange={(event) => setModel(overrideFor(event.target.value, ownModel))}
            >
              {modelOptions(providers, ownModel, model, seatModels).map((row) => (
                <option
                  key={row.value}
                  value={row.value}
                  title={row.spec ? 'Where the harness starts unless a run moves it' : undefined}
                >
                  {row.label}
                </option>
              ))}
            </select>
            <div style={{ fontSize: 12, color: 'var(--mut)', marginTop: 5 }}>
              The new run use it from the first replayed turn onward, journalled as a
              model swap. Changing exactly one variable is the point.
            </div>
          </div>
        </div>

        <div style={{ display: 'flex', gap: 9, marginTop: 22, justifyContent: 'flex-end' }}>
          <button className="v-btn v-btn-ghost" onClick={onClose}>
            Cancel
          </button>
          <button
            className="v-btn v-btn-primary"
            disabled={messages.length === 0}
            onClick={() => onBranch(messages, model)}
          >
            <i className="ph ph-git-branch" />
            Branch the run
          </button>
        </div>
      </div>
    </div>
  )
}

/**
 * Provider message blocks, flattened to the text a thread can replay.
 *
 * A recorded message's content is either a string or a list of typed blocks;
 * only the text ones survive a replay, because a tool-use block refers to a
 * call id from a run that has already finished. Dropping them is what keeps
 * the replayed conversation valid rather than half-referring to a dead run.
 */
function flatten(messages: Record<string, unknown>[]): { role: string; content: string }[] {
  const rows: { role: string; content: string }[] = []
  for (const message of messages) {
    const role = String(message.role ?? 'user')
    const content = message.content
    if (typeof content === 'string') {
      rows.push({ role, content })
      continue
    }
    if (!Array.isArray(content)) continue
    const text = content
      .map((block) => {
        if (typeof block === 'string') return block
        const record = block as Record<string, unknown>
        return record.type === 'text' && typeof record.text === 'string' ? record.text : ''
      })
      .filter(Boolean)
      .join('\n')
    if (text) rows.push({ role, content: text })
  }
  return rows
}
