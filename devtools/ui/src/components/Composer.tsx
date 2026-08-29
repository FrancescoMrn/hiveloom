/**
 * The workspace composer: one input under both views, the way an agent shell
 * puts it under the conversation.
 *
 * Three things live here that only make sense at the point of sending. What a
 * message *does* while a run is in flight — hold until the run ends, or reach
 * this one at its next turn boundary — is a choice, not a mode the shell can
 * guess, so it is a visible toggle rather than a hidden rule. Which model the
 * workspace runs on sits beside it because changing one variable and re-running
 * is the loop this tool exists for, and doing it by rewriting the spec would
 * change every other conversation too. And the meter is the run's own
 * accounting, read off the journal as it arrives.
 *
 * Attachments are written into the harness's workspace before the turn is
 * sent, and the message names their paths: hiveloom has no attachment concept,
 * but every harness with `file_read` can open a file in its own directory.
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api'
import { modelOptions, overrideFor } from '../models'
import type { TaskGuide } from '../taskGuide'
import { formatTokens, projectTrajectory } from '../trajectory'
import type { DeliverMode } from '../prefs'
import type { Attachment, Provider } from '../types'
import type { RunWorkspace } from '../useRun'

const MODES: { id: DeliverMode; label: string; icon: string; title: string }[] = [
  {
    id: 'queue',
    label: 'Queue',
    icon: 'ph-stack-plus',
    title: 'Hold it until the run finishes — the default',
  },
  {
    id: 'steer',
    label: 'Steer',
    icon: 'ph-arrow-bend-down-right',
    title: 'Inject it at the next turn boundary of the running turn',
  },
]

export function Composer({
  harnessId,
  harnessName,
  ownModel,
  seatModels = [],
  workspace,
  providers,
  deliver,
  onDeliver,
  disabled,
  focusToken = 0,
  variant = 'bar',
  children,
  taskGuide,
}: {
  harnessId: string
  harnessName: string
  /** `provider/id` from the spec: the model a run uses with no override. */
  ownModel: string
  /** The seat's shortlist for this harness; empty means every usable model. */
  seatModels?: string[]
  workspace: RunWorkspace
  providers: Provider[] | null
  /** What Enter does while a run is live; ⌘/Ctrl+Enter always does the other. */
  deliver: DeliverMode
  onDeliver: (mode: DeliverMode) => void
  disabled?: boolean
  /** Changes when the shell wants the caret here — starting a new run. */
  focusToken?: number
  /** `hero` is the empty-workspace state: centred, with the seats above it. */
  variant?: 'bar' | 'hero'
  children?: React.ReactNode
  /** Guidance derived from the harness's own task contract, never a UI copy. */
  taskGuide?: TaskGuide
}) {
  const [draft, setDraft] = useState('')
  const [attachments, setAttachments] = useState<Attachment[]>([])
  const [uploadError, setUploadError] = useState<string | null>(null)
  const [uploading, setUploading] = useState(false)
  const [inputError, setInputError] = useState<string | null>(null)
  const input = useRef<HTMLTextAreaElement | null>(null)
  const picker = useRef<HTMLInputElement | null>(null)
  const busy = workspace.busy

  useEffect(() => {
    if (focusToken > 0) input.current?.focus()
  }, [focusToken])

  const meter = useMemo(() => summarise(workspace), [workspace])

  /** The models this seat offers: usable, narrowed by the harness's shortlist. */
  const picks = useMemo(
    () => modelOptions(providers, ownModel, workspace.model, seatModels),
    [providers, ownModel, workspace.model, seatModels],
  )

  const chooseModel = (selector: string) => {
    if (!busy) {
      workspace.setModel(overrideFor(selector, ownModel))
      return
    }
    // Mid-run there is no "no override" to express: the loop is told a
    // provider and an id, so the spec's own model is sent as explicitly as
    // any other.
    const [provider, ...rest] = selector.split('/')
    if (rest.length) void workspace.switchModel({ provider, model: rest.join('/') })
  }

  /**
   * Messages written while a run is live and held for *after* it.
   *
   * Held here rather than sent, because hiveloom has exactly one mid-run
   * delivery — an operator message at the next turn boundary — and that is
   * what Steer already is. Queue is the other thing a person means: not now,
   * next turn. The held text stays visible and cancellable, and the run ending
   * is what releases it.
   */
  const [held, setHeld] = useState<{ id: string; content: string; files: Attachment[] }[]>([])
  const heldSeq = useRef(0)
  const wasBusy = useRef(busy)

  useEffect(() => {
    const finished = wasBusy.current && !busy
    wasBusy.current = busy
    if (!finished || held.length === 0 || disabled) return
    const [next, ...rest] = held
    setHeld(rest)
    void workspace.send(next.content, next.files)
  }, [busy, disabled, held, workspace])

  const submit = async (mode: DeliverMode) => {
    const text = draft.trim()
    if (!text || disabled) return
    const files = attachments
    // Steering is an operator intervention, not a new task statement. Every
    // ordinary send is checked against the task contract before it can spend
    // a model call; exact-input harnesses are especially easy to mistake for
    // a general chat box.
    if (!busy) {
      const issue = taskGuide?.validate(text, files) ?? null
      if (issue) {
        setInputError(issue)
        return
      }
    }
    setInputError(null)
    setDraft('')
    setAttachments([])

    if (!busy) {
      await workspace.send(text, files)
      return
    }
    // While a run is live "send" has two meanings and they are not
    // interchangeable: steering reaches *this* run at its next turn boundary;
    // queueing waits for the next one. The button says which it is doing.
    if (mode === 'steer') {
      // A steer is text injected into a running context — there is nowhere
      // for a file list to go, so any attachments stay staged for the turn
      // that follows rather than being silently dropped.
      setAttachments(files)
      await workspace.steer(text)
      return
    }
    heldSeq.current += 1
    setHeld((current) => [...current, { id: `held_${heldSeq.current}`, content: text, files }])
  }

  const attach = async (files: FileList | null) => {
    if (!files || files.length === 0) return
    setUploading(true)
    setUploadError(null)
    try {
      const added: Attachment[] = []
      for (const file of Array.from(files)) added.push(await api.upload(harnessId, file))
      setAttachments((current) => [...current, ...added])
    } catch (exc) {
      setUploadError(String(exc))
    } finally {
      setUploading(false)
      if (picker.current) picker.current.value = ''
    }
  }

  const sendIcon = !busy
    ? 'ph-arrow-up'
    : deliver === 'steer'
      ? 'ph-arrow-bend-down-right'
      : 'ph-stack-plus'
  const sendTitle = !busy
    ? 'Run the harness with this thread as history'
    : deliver === 'steer'
      ? 'Deliver at the next turn boundary of the running turn'
      : 'Add to the queue for after this run'

  return (
    <div className={variant === 'hero' ? 'composer-hero' : 'composer-bar'}>
      {children}

      {busy && workspace.queued.length > 0 && (
        <div className="queue">
          <div className="steer-note">
            <i className="ph ph-arrow-bend-down-right" />
            {workspace.queued.length === 1
              ? 'Steering this run at its next turn boundary'
              : `${workspace.queued.length} steering messages for this run's next turn boundaries`}
          </div>
          {workspace.queued.map((message) => (
            <QueuedMessage
              key={message.id}
              content={message.content}
              onEdit={(text) => workspace.editQueued(message.id, text)}
              onDrop={() => workspace.dropQueued(message.id)}
            />
          ))}
        </div>
      )}

      {held.length > 0 && (
        <div className="queue">
          <div className="steer-note held">
            <i className="ph ph-stack-plus" />
            {held.length === 1
              ? 'Held for the next turn — sent when this run finishes'
              : `${held.length} messages held for the turns after this run`}
          </div>
          {held.map((message) => (
            <QueuedMessage
              key={message.id}
              content={message.content}
              onEdit={async (text) =>
                setHeld((current) =>
                  current.map((item) => (item.id === message.id ? { ...item, content: text } : item)),
                )
              }
              onDrop={async () =>
                setHeld((current) => current.filter((item) => item.id !== message.id))
              }
            />
          ))}
        </div>
      )}

      {uploadError && (
        <div className="composer-error">
          <i className="ph ph-warning" />
          <span>{uploadError}</span>
          <button onClick={() => setUploadError(null)} title="Dismiss">
            <i className="ph ph-x" />
          </button>
        </div>
      )}

      {inputError && (
        <div className="composer-guidance-error" role="alert">
          <i className="ph ph-info" />
          <span>{inputError}</span>
          <button onClick={() => setInputError(null)} title="Dismiss">
            <i className="ph ph-x" />
          </button>
        </div>
      )}

      <div className="composer-box" data-busy={busy ? '1' : '0'}>
        {attachments.length > 0 && (
          <div className="attach-row">
            {attachments.map((file) => (
              <span key={file.path} className="attach-chip mono" title={file.path}>
                <i className="ph ph-file-text" />
                {file.name}
                <span className="attach-size">{formatBytes(file.bytes)}</span>
                <button
                  title="Remove from this turn — the file stays in the workspace"
                  onClick={() =>
                    setAttachments((current) => current.filter((item) => item.path !== file.path))
                  }
                >
                  <i className="ph ph-x" />
                </button>
              </span>
            ))}
          </div>
        )}

        <div className="composer-line">
          <input
            ref={picker}
            type="file"
            multiple
            className="sr-only"
            onChange={(event) => void attach(event.target.files)}
          />
          <button
            className="attach-btn"
            disabled={disabled || uploading}
            title="Attach files — written into the harness workspace and named in the turn"
            onClick={() => picker.current?.click()}
          >
            <i className={`ph ${uploading ? 'ph-circle-notch' : 'ph-plus'}`} />
          </button>
          <textarea
            ref={input}
            className="composer-input"
            rows={variant === 'hero' ? 3 : 1}
            value={draft}
            disabled={disabled}
            placeholder={
              disabled
                ? 'This harness cannot run yet'
                : busy
                  ? deliver === 'steer'
                    ? 'Steer the run — delivered at the next turn boundary…'
                    : 'Queue a message for after this run…'
                  : taskGuide?.placeholder ?? `Task for ${harnessName}…    ⌘/Ctrl + Enter`
            }
            onChange={(event) => {
              setDraft(event.target.value)
              if (inputError) setInputError(null)
            }}
            onKeyDown={(event) => {
              if (event.key !== 'Enter') return
              // ⌘/Ctrl + Enter always takes the *other* delivery, so the
              // one-off never costs a trip to the toggle.
              if (event.metaKey || event.ctrlKey) {
                event.preventDefault()
                void submit(busy ? other(deliver) : deliver)
              }
            }}
          />
          <button
            className="seat-send"
            onClick={() => void submit(deliver)}
            disabled={!draft.trim() || disabled}
            title={sendTitle}
          >
            <i className={`ph ${sendIcon}`} />
          </button>
        </div>

        <div className="composer-controls">
          {busy && (
            <div
              className="mode-toggle"
              title="How the next message reaches a run already in flight"
            >
              {MODES.map((mode) => (
                <button
                  key={mode.id}
                  data-on={deliver === mode.id ? '1' : '0'}
                  title={mode.title}
                  onClick={() => onDeliver(mode.id)}
                >
                  <i className={`ph ${mode.icon}`} />
                  {mode.label}
                </button>
              ))}
            </div>
          )}

          {/* Picked models are shown, not hidden behind a dropdown: choosing
              a handful in Settings is what makes them worth a row of their own,
              and switching model between turns is the loop this tool exists
              for. With no shortlist the directory is too long for that, so it
              stays a select. */}
          {seatModels.length > 0 && picks.length > 1 ? (
            <div
              className="model-picks"
              title={
                busy
                  ? 'Move this run onto another model at its next turn boundary'
                  : 'Model this workspace runs on — the harness on disk is not changed'
              }
            >
              <i className="ph ph-brain" style={{ color: 'var(--evo)' }} />
              {picks.map((row) => (
                <button
                  key={row.value}
                  className="mono"
                  data-on={(workspace.model || ownModel) === row.value ? '1' : '0'}
                  title={
                    row.spec
                      ? `${row.value} — where ${harnessName} starts unless a run moves it`
                      : row.value
                  }
                  onClick={() => chooseModel(row.value)}
                >
                  {row.value.split('/').slice(1).join('/') || row.value}
                </button>
              ))}
            </div>
          ) : (
            <label
              className="model-seat"
              title={
                busy
                  ? 'Move this run onto another model at its next turn boundary'
                  : 'Model this workspace runs on — the harness on disk is not changed'
              }
            >
              <i className="ph ph-brain" style={{ color: 'var(--evo)' }} />
              <select
                className="mono"
                /* The model in force: the workspace's override, or the one the
                   spec names. A harness is not bound to a model — the spec only
                   says where a run starts — so the seat shows a model either
                   way rather than a stand-in for one. */
                value={workspace.model || ownModel}
                onChange={(event) => chooseModel(event.target.value)}
              >
                {/* Models, and only models — see models.ts. */}
                {picks.map((row) => (
                  <option
                    key={row.value}
                    value={row.value}
                    title={row.spec ? `Where ${harnessName} starts unless a run moves it` : undefined}
                  >
                    {row.label}
                  </option>
                ))}
              </select>
            </label>
          )}

          {busy && (
            <button
              className="seat-btn stop"
              onClick={() => void workspace.stop()}
              title="Stop gracefully at the next turn boundary — the journal stays intact"
            >
              <i className="ph ph-stop" />
              Stop
            </button>
          )}
        </div>
      </div>

      {variant === 'bar' && (
        <div className="composer-meter mono">
          <span title="Input tokens billed on this run so far">in {meter.input}</span>
          <span title="Output tokens billed on this run so far">out {meter.output}</span>
          <span className="sep" />
          <span title="Turn the loop is on">turn {meter.turn}</span>
          <span title="Model, tool and verifier steps recorded in this turn">
            {meter.steps} steps
          </span>
          {busy && (
            <span className="live">
              <span className="dot" />
              live
            </span>
          )}
        </div>
      )}
    </div>
  )
}

/** A message the loop has not consumed yet: still editable, still cancellable. */
function QueuedMessage({
  content,
  onEdit,
  onDrop,
}: {
  content: string
  onEdit: (text: string) => Promise<void>
  onDrop: () => Promise<void>
}) {
  const [editing, setEditing] = useState(false)
  const [text, setText] = useState(content)
  const [error, setError] = useState<string | null>(null)

  const commit = async () => {
    try {
      await onEdit(text.trim())
      setEditing(false)
      setError(null)
    } catch {
      // The only reason an edit fails: the agent already has it.
      setError('already delivered')
      setEditing(false)
    }
  }

  return (
    <div className="queued" data-error={error ? '1' : '0'}>
      {editing ? (
        <input
          className="queued-input"
          autoFocus
          value={text}
          onChange={(event) => setText(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') void commit()
            if (event.key === 'Escape') setEditing(false)
          }}
        />
      ) : (
        <span className="queued-text">{content}</span>
      )}
      {error && <span className="queued-error">{error}</span>}
      <button onClick={() => (editing ? void commit() : setEditing(true))} title="Edit">
        <i className={`ph ${editing ? 'ph-check' : 'ph-pencil-simple'}`} />
      </button>
      <button
        onClick={() => void onDrop().catch(() => setError('already delivered'))}
        title="Remove from the queue"
      >
        <i className="ph ph-x" />
      </button>
    </div>
  )
}

function other(mode: DeliverMode): DeliverMode {
  return mode === 'queue' ? 'steer' : 'queue'
}

/**
 * The turn's own accounting, from the journal rather than an estimate.
 *
 * Zeroes while a turn is still opening: the numbers only exist once a model
 * call has closed, and a made-up running total would be the one thing on this
 * bar that is not measured.
 */
function summarise(workspace: RunWorkspace): {
  input: string
  output: string
  turn: string
  steps: string
} {
  const last = [...workspace.turns].reverse().find((turn) => turn.role === 'assistant')
  const events = last?.events ?? []
  if (events.length === 0) {
    return { input: '0', output: '0', turn: String(last?.result?.turns ?? 1), steps: '0' }
  }
  const projection = projectTrajectory(events)
  return {
    input: formatTokens(projection.totals.usage.input_tokens),
    output: formatTokens(projection.totals.usage.output_tokens),
    turn: String(Math.max(1, projection.totals.turns)),
    steps: String(projection.spans.length),
  }
}

function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${(size / (1024 * 1024)).toFixed(1)} MB`
}
