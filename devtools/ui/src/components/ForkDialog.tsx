/**
 * Fork a run at one of its model calls.
 *
 * A fork re-enters the run at a recorded boundary with the conversation up to
 * that point replayed, so you can change one variable — usually the model —
 * and compare the evidence. Only model calls are valid boundaries: a tool
 * result is mid-turn, and re-entering there would mean inventing the request
 * that followed it.
 *
 * The target directory is chosen by the server under the parent harness's
 * protected `.hiveloom/forks` directory. The browser supplies a name, never a
 * path.
 */
import { useEffect, useState } from 'react'
import { api } from '../api'
import type { ForkPoint, ForkResult, Provider } from '../types'
import { Label, Notice } from './common'

export function ForkDialog({
  runId,
  point,
  onClose,
  onOpenHarness,
}: {
  runId: string
  point: ForkPoint
  onClose: () => void
  /** Select the fork in the rail — it is registered as it is created. */
  onOpenHarness: (harnessId: string) => Promise<void>
}) {
  const short = runId.replace(/^run_/, '').slice(0, 8)
  const [name, setName] = useState(`${short}-turn${point.turn}`)
  const [override, setOverride] = useState('')
  const [providers, setProviders] = useState<Provider[] | null>(null)
  const [result, setResult] = useState<ForkResult | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    api.providers().then(setProviders).catch(() => setProviders([]))
  }, [])

  const submit = async () => {
    setBusy(true)
    setError(null)
    try {
      const selector = override.trim()
      const provider = providers?.find((item) => selector.startsWith(`${item.name}/`))
      setResult(
        await api.fork(runId, {
          at: point.seq,
          name: name.trim(),
          model: provider ? selector.slice(provider.name.length + 1) : selector || undefined,
          provider: provider?.name,
        }),
      )
    } catch (exc) {
      setError(String(exc))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="scrim" onClick={onClose}>
      <div className="v-panel modal rise" onClick={(event) => event.stopPropagation()}>
        <h2 style={{ fontSize: 17 }}>Fork at turn {point.turn}</h2>
        <p style={{ color: 'var(--dim)', fontSize: 13, marginTop: 6 }}>
          Re-enters this run at seq {point.seq} — the {point.phase} model call with{' '}
          {point.num_messages} message{point.num_messages === 1 ? '' : 's'} of history — as a new
          harness fork contained by this one. The parent run is never modified.
        </p>

        {result ? (
          <>
            <div style={{ marginTop: 18 }}>
              <Notice
                icon="ph-git-fork"
                tone="ok"
                title="Fork created and registered"
                body={`${result.messages} messages replayed at turn ${result.turn}. It is in the rail now, under its own folder — it shares the parent's name, so the folder is what tells them apart.`}
              />
            </div>
            <dl className="event-fields" style={{ marginTop: 14 }}>
              <div>
                <dt>directory</dt>
                <dd className="mono">{result.directory}</dd>
              </div>
              <div>
                <dt>version</dt>
                <dd className="mono">{result.version_hash.slice(0, 12)}</dd>
              </div>
              {result.model_override && (
                <div>
                  <dt>model override</dt>
                  <dd className="mono">
                    {result.model_override.from} &rarr;{' '}
                    {result.model_override.provider}:{result.model_override.model}
                  </dd>
                </div>
              )}
              <div>
                <dt>trust</dt>
                <dd>{result.trust_inherited ? 'inherited from the parent' : 'not trusted yet'}</dd>
              </div>
            </dl>
            {result.warnings.length > 0 && (
              <div style={{ marginTop: 14 }}>
                <Notice
                  icon="ph-warning"
                  tone="warn"
                  title="Warnings"
                  body={result.warnings.join(' · ')}
                />
              </div>
            )}
          </>
        ) : (
          <div style={{ marginTop: 20, display: 'grid', gap: 14 }}>
            <div>
              <Label>Folder name</Label>
              <input
                className="v-input mono"
                autoFocus
                value={name}
                style={{ marginTop: 6, fontSize: 13 }}
                onChange={(event) => setName(event.target.value)}
              />
              <div style={{ fontSize: 12, color: 'var(--mut)', marginTop: 5 }}>
                Created under the parent harness at <code>.hiveloom/forks/</code>. Letters,
                digits, <code>.</code>, <code>_</code> and <code>-</code> only.
              </div>
            </div>

            <div>
              <Label>Model override (optional)</Label>
              <input
                className="v-input mono"
                value={override}
                placeholder="leave empty to keep the parent's model"
                list="fork-models"
                style={{ marginTop: 6, fontSize: 13 }}
                onChange={(event) => setOverride(event.target.value)}
              />
              <datalist id="fork-models">
                {(providers ?? []).flatMap((provider) =>
                  provider.models.map((model) => (
                    <option
                      key={`${provider.name}/${model.id}`}
                      value={`${provider.name}/${model.id}`}
                    />
                  )),
                )}
              </datalist>
              <div style={{ fontSize: 12, color: 'var(--mut)', marginTop: 5 }}>
                Changing exactly one variable is the point of a fork — this is usually it.
              </div>
            </div>
          </div>
        )}

        {error && (
          <div style={{ marginTop: 16 }}>
            <Notice icon="ph-warning-octagon" tone="err" title="Could not fork" body={error} />
          </div>
        )}

        <div style={{ display: 'flex', gap: 9, marginTop: 22, justifyContent: 'flex-end' }}>
          <button className="v-btn v-btn-ghost" onClick={onClose} disabled={busy}>
            {result ? 'Done' : 'Cancel'}
          </button>
          {result?.harness_id && (
            <button
              className="v-btn v-btn-primary"
              onClick={() => void onOpenHarness(result.harness_id)}
            >
              <i className="ph ph-arrow-right" />
              Open the fork
            </button>
          )}
          {!result && (
            <button
              className="v-btn v-btn-primary"
              onClick={() => void submit()}
              disabled={!name.trim() || busy}
            >
              {busy ? (
                <i className="ph ph-circle-notch" style={{ animation: 'spin 1s linear infinite' }} />
              ) : (
                <i className="ph ph-git-fork" />
              )}
              Create fork
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
