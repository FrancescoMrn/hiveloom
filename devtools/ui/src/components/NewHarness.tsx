import { useState } from 'react'
import { api } from '../api'
import { Label, Notice } from './common'

/**
 * Create a harness: `hiveloom init`, plus the register-and-trust steps you would
 * otherwise do at a terminal before it became usable here.
 *
 * The result is a minimal *valid* skeleton, not a finished harness — the Spec
 * tab opens next, which is where the work actually happens.
 */
export function NewHarness({
  trustOnCreate,
  onClose,
  onCreated,
}: {
  /** From Settings. Code hooks run with your privileges, so it stays a choice. */
  trustOnCreate: boolean
  onClose: () => void
  onCreated: (id: string) => Promise<void>
}) {
  const [name, setName] = useState('')
  const [task, setTask] = useState('')
  const [directory, setDirectory] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Typing a name proposes a directory beside the example harnesses, which is
  // where a new one usually belongs; editing the directory stops the guessing.
  const [dirTouched, setDirTouched] = useState(false)
  const slug = name
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, '-')
    .replace(/^-|-$/g, '')
  const resolvedDir = dirTouched ? directory : slug ? `harnesses/${slug}` : ''

  const submit = async () => {
    setBusy(true)
    setError(null)
    try {
      const created = await api.create({
        directory: resolvedDir,
        name: name.trim(),
        task: task.trim(),
        trust: trustOnCreate,
      })
      await onCreated(created.id)
    } catch (exc) {
      setError(String(exc))
      setBusy(false)
    }
  }

  const ready = name.trim() && task.trim() && resolvedDir

  return (
    <div className="scrim" onClick={onClose}>
      <div className="v-panel modal rise" onClick={(e) => e.stopPropagation()}>
        <h2 style={{ fontSize: 17 }}>New harness</h2>
        <p style={{ color: 'var(--dim)', fontSize: 13, marginTop: 6 }}>
          Creates a minimal valid skeleton and registers it
          {trustOnCreate
            ? ', and trusts it so it can run straight away.'
            : '. It will not run until you trust it — Settings decides which.'}
        </p>

        <div style={{ marginTop: 20, display: 'grid', gap: 14 }}>
          <div>
            <Label>Name</Label>
            <input
              className="v-input"
              autoFocus
              value={name}
              placeholder="support-triage"
              onChange={(e) => setName(e.target.value)}
              style={{ marginTop: 6 }}
            />
          </div>

          <div>
            <Label>Task</Label>
            <input
              className="v-input"
              value={task}
              placeholder="Route an inbound support email to the right queue."
              onChange={(e) => setTask(e.target.value)}
              style={{ marginTop: 6 }}
            />
            <div style={{ fontSize: 12, color: 'var(--mut)', marginTop: 5 }}>
              One line. It becomes the harness description.
            </div>
          </div>

          <div>
            <Label>Directory</Label>
            <input
              className="v-input mono"
              value={resolvedDir}
              placeholder="harnesses/support-triage"
              onChange={(e) => {
                setDirTouched(true)
                setDirectory(e.target.value)
              }}
              style={{ marginTop: 6, fontSize: 13 }}
            />
            <div style={{ fontSize: 12, color: 'var(--mut)', marginTop: 5 }}>
              Relative to where the API process was started.
            </div>
          </div>
        </div>

        {error && (
          <div style={{ marginTop: 16 }}>
            <Notice icon="ph-warning-octagon" tone="err" title="Could not create" body={error} />
          </div>
        )}

        <div style={{ display: 'flex', gap: 9, marginTop: 22, justifyContent: 'flex-end' }}>
          <button className="v-btn v-btn-ghost" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button
            className="v-btn v-btn-primary"
            onClick={() => void submit()}
            disabled={!ready || busy}
          >
            {busy ? (
              <i className="ph ph-circle-notch" style={{ animation: 'spin 1s linear infinite' }} />
            ) : (
              <i className="ph ph-plus" />
            )}
            Create
          </button>
        </div>
      </div>
    </div>
  )
}
