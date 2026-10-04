import { useEffect, useRef, useState } from 'react'
import logo from '../../../../docs/assets/logo.png'
import { projectTrajectory } from '../trajectory'
import { api } from '../api'
import { runLabel } from '../runs'
import type { Artifact, Attachment, CopilotInfo, Harness, RunRow, WorkbenchDirectory } from '../types'
import type { CopilotWorkspace } from '../useCopilot'
import { DelegationTrail, MessageBody } from './messages'
import { StatusPill } from './common'
import { ModelPicker } from './ModelPicker'

const ARTIFACT_LABELS: Record<string, { icon: string; title: string }> = {
  workspace_context: { icon: 'ph-crosshair', title: 'Workbench context' },
  harness_catalog: { icon: 'ph-hexagon', title: 'Harnesses' },
  harness_contract: { icon: 'ph-file-code', title: 'Harness contract' },
  harness_file: { icon: 'ph-file-text', title: 'Attached file' },
  memories: { icon: 'ph-brain', title: 'Memory' },
  memory_saved: { icon: 'ph-bookmark-simple', title: 'Remembered' },
  memory_forgotten: { icon: 'ph-eraser', title: 'Forgotten' },
  harness_created: { icon: 'ph-check-circle', title: 'Harness created' },
  validation: { icon: 'ph-shield-check', title: 'Validation' },
  dry_run: { icon: 'ph-test-tube', title: 'Dry run' },
  target_run: { icon: 'ph-play-circle', title: 'Target run' },
  recent_runs: { icon: 'ph-clock-counter-clockwise', title: 'Recent runs' },
  run_evidence: { icon: 'ph-list-magnifying-glass', title: 'Run evidence' },
  harness_stats: { icon: 'ph-chart-line-up', title: 'Harness results' },
  version_comparison: { icon: 'ph-git-diff', title: 'Version comparison' },
  improvement_proposal: { icon: 'ph-sparkle', title: 'Improvement proposal' },
  interface: { icon: 'ph-browser', title: 'Standalone interface' },
  research_program: { icon: 'ph-flask', title: 'Research program' },
}

export function CopilotChat({
  info,
  harness,
  harnesses = [],
  onSelectHarness,
  run,
  workspace,
  directory = null,
  model,
  onModel,
  onArtifact,
  onDetachRun,
  onOpenRun,
}: {
  info: CopilotInfo | null
  harness: Harness | null
  /** Offered as one-click choices while no harness is attached. */
  harnesses?: Harness[]
  onSelectHarness?: (id: string) => void
  run: RunRow | null
  workspace: CopilotWorkspace
  /** The workbench's providers: the composer offers exactly their models. */
  directory?: WorkbenchDirectory | null
  model: string
  onModel: (model: string) => void
  onArtifact: (artifact: Artifact) => void
  onDetachRun: () => void
  /** Navigate to a run id (a delegated child run); the trail is hidden without it. */
  onOpenRun?: (runId: string) => void
}) {
  const empty = workspace.messages.length === 0
  const [draft, setDraft] = useState('')
  const [attachments, setAttachments] = useState<Attachment[]>([])
  const [attachmentError, setAttachmentError] = useState<string | null>(null)
  // Set when + is pressed with nothing attached: files land in a harness
  // folder, so the composer asks for one instead of failing.
  const [needsHarness, setNeedsHarness] = useState(false)
  const [uploading, setUploading] = useState(false)
  const input = useRef<HTMLTextAreaElement | null>(null)
  const fileInput = useRef<HTMLInputElement | null>(null)
  const end = useRef<HTMLDivElement | null>(null)

  // Whatever the composer said about the previous harness (or its absence)
  // no longer applies once the context changes.
  useEffect(() => {
    setNeedsHarness(false)
    setAttachmentError(null)
  }, [harness?.id])

  useEffect(() => {
    end.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [workspace.messages, workspace.busy])

  // The composer opens at one line so a short question sits on the buttons'
  // optical line rather than at the top of an empty second row, and grows with
  // the draft up to the stylesheet's max-height, after which it scrolls.
  useEffect(() => {
    const area = input.current
    if (!area) return
    area.style.height = 'auto'
    area.style.height = `${area.scrollHeight}px`
  }, [draft])

  const submit = async (value = draft) => {
    const text = value.trim()
    if (!text || workspace.busy) return
    setDraft('')
    const files = attachments
    setAttachments([])
    await workspace.send(text, files)
  }

  const attach = async (files: FileList | null) => {
    if (!files?.length || !harness) return
    setUploading(true)
    setAttachmentError(null)
    try {
      const uploaded: Attachment[] = []
      for (const file of Array.from(files)) uploaded.push(await api.upload(harness.id, file))
      setAttachments((current) => [...current, ...uploaded])
    } catch (exc) {
      setAttachmentError(String(exc))
    } finally {
      setUploading(false)
      if (fileInput.current) fileInput.current.value = ''
    }
  }

  return (
    <section className="copilot-chat" data-empty={empty ? '1' : '0'}>
      <div className="copilot-thread">
        {empty ? (
          <div className="copilot-welcome">
            <img className="copilot-welcome-mark" src={logo} alt="Hiveloom" width={42} height={42} />
            <h1>What are we working on today?</h1>
            {!harness && onSelectHarness && (
              <HarnessChoices
                lead={harnesses.length ? 'Pick a harness to work on, or ask Hiveloom to create one.' : 'No harnesses yet — ask Hiveloom to create one.'}
                harnesses={harnesses}
                onSelect={onSelectHarness}
              />
            )}
          </div>
        ) : (
          workspace.messages.map((message, index) =>
            message.role === 'user' ? (
              <div className="copilot-message user rise" key={index}>
                <div>
                  <span>{message.content}</span>
                  {(message.attachments ?? []).map((attachment) => (
                    <span className="message-attachment" key={`${attachment.path}-${attachment.sha256}`}>
                      <i className="ph ph-paperclip" /> {attachment.name}
                    </span>
                  ))}
                </div>
              </div>
            ) : (
              <div className="copilot-message assistant rise" key={index}>
                <div className="copilot-who">
                  <img src={logo} alt="" width={18} height={18} />
                  <strong>Hiveloom</strong>
                </div>
                <CopilotActivity events={message.events ?? []} live={workspace.busy && index === workspace.messages.length - 1} />
                {message.error ? (
                  <div className="copilot-error">{message.error}</div>
                ) : message.content ? (
                  <MessageBody text={message.content} />
                ) : workspace.busy ? (
                  <div className="copilot-thinking">
                    <i className="ph ph-circle-notch" /> Thinking with the framework…
                  </div>
                ) : null}
                {(message.artifacts ?? []).length > 0 && (
                  <div className="artifact-grid">
                    {message.artifacts!.map((artifact, artifactIndex) => (
                      <ArtifactCard
                        key={`${artifact.kind}-${artifactIndex}`}
                        artifact={artifact}
                        onOpen={() => onArtifact(artifact)}
                      />
                    ))}
                  </div>
                )}
                {message.result && onOpenRun && (
                  <DelegationTrail result={message.result} onOpenRun={onOpenRun} />
                )}
                {message.result && (
                  <div className="copilot-run-meta mono">
                    <StatusPill status={message.result.status} />
                    <span>{message.result.turns} turns</span>
                    <span>${message.result.cost_usd.toFixed(4)}</span>
                    {(message.result.delegated_cost_usd ?? 0) > 0 && (
                      <span className="delegated-cost">
                        incl. ${message.result.delegated_cost_usd!.toFixed(4)} delegated
                      </span>
                    )}
                    <span>{message.result.duration_seconds.toFixed(1)}s</span>
                  </div>
                )}
              </div>
            ),
          )
        )}
        <div ref={end} />
      </div>

      <div className="copilot-composer-wrap">
        {(harness || run) && (
          <div className="copilot-context-row">
            <span>Context</span>
            {harness && (
              <span className="copilot-context-chip">
                <i className="ph ph-hexagon" /> {harness.name}
              </span>
            )}
            {run && (
              <span className="copilot-context-chip" title={run.run_id}>
                <i className="ph ph-list-magnifying-glass" /> {runLabel(run)}
                <button
                  className="copilot-context-remove"
                  onClick={onDetachRun}
                  title="Remove this run from chat context"
                  aria-label={`Remove ${runLabel(run)} from chat context`}
                >
                  <i className="ph ph-x" />
                </button>
              </span>
            )}
          </div>
        )}
        {attachments.length > 0 && (
          <div className="copilot-attachment-row">
            {attachments.map((attachment, index) => (
              <span className="copilot-attachment" key={`${attachment.path}-${index}`}>
                <i className="ph ph-file" />
                <span className="ellipsis">{attachment.name}</span>
                <button
                  onClick={() => setAttachments((current) => current.filter((_, item) => item !== index))}
                  title={`Remove ${attachment.name}`}
                >
                  <i className="ph ph-x" />
                </button>
              </span>
            ))}
          </div>
        )}
        <div className="copilot-composer">
          <input
            ref={fileInput}
            type="file"
            multiple
            hidden
            onChange={(event) => void attach(event.target.files)}
          />
          <button
            className="composer-tool"
            disabled={workspace.busy || uploading}
            onClick={() => {
              if (!harness) {
                setNeedsHarness(true)
                return
              }
              fileInput.current?.click()
            }}
            title={harness ? `Add files to ${harness.name}` : 'Select a harness before adding files'}
            aria-label={harness ? `Add files to ${harness.name}` : 'Add files'}
          >
            <i className={`ph ${uploading ? 'ph-circle-notch spin' : 'ph-plus'}`} />
          </button>
          <textarea
            ref={input}
            rows={1}
            value={draft}
            disabled={workspace.busy}
            placeholder="Ask Hiveloom to build, run, explain, improve, or create an interface…"
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
                event.preventDefault()
                void submit()
              }
            }}
          />
          {workspace.busy ? (
            <button className="copilot-stop" onClick={() => void workspace.stop()} title="Stop gracefully">
              <i className="ph ph-stop" />
            </button>
          ) : (
            <button className="seat-send" disabled={!draft.trim()} onClick={() => void submit()} title="Send to Hiveloom">
              <i className="ph ph-arrow-up" />
            </button>
          )}
        </div>
        {attachmentError && <div className="copilot-attachment-error">{attachmentError}</div>}
        {needsHarness && !harness && onSelectHarness && (
          <div className="copilot-needs-harness">
            <HarnessChoices
              lead={harnesses.length ? 'Files are added to a harness folder. Choose one first:' : 'Files are added to a harness folder — create a harness first.'}
              harnesses={harnesses}
              onSelect={onSelectHarness}
            />
            <button className="icon-btn" onClick={() => setNeedsHarness(false)} title="Dismiss">
              <i className="ph ph-x" />
            </button>
          </div>
        )}
        <div className="copilot-composer-tools">
          <ModelPicker
            variant="chip"
            directory={directory}
            value={model}
            ariaLabel="Copilot model"
            onChange={onModel}
          />
          <span>⌘/Ctrl + Enter · framework actions are journalled</span>
        </div>
      </div>
      {empty && (
        <div className="copilot-empty-actions">
          {(info?.suggestions ?? []).map((suggestion) => (
            <button key={suggestion} onClick={() => void submit(suggestion)}>
              <i className="ph ph-arrow-up-right" />
              <span>{suggestion}</span>
            </button>
          ))}
        </div>
      )}
    </section>
  )
}

const MAX_HARNESS_CHOICES = 6

function HarnessChoices({ lead, harnesses, onSelect }: { lead: string; harnesses: Harness[]; onSelect: (id: string) => void }) {
  const trunks = harnesses.filter((item) => !item.is_fork)
  const shown = trunks.slice(0, MAX_HARNESS_CHOICES)
  return (
    <div className="harness-choices">
      <span>{lead}</span>
      {shown.length > 0 && (
        <div>
          {shown.map((item) => (
            <button key={item.id} onClick={() => onSelect(item.id)} title={item.description || item.name}>
              <i className="ph ph-hexagon" />
              {item.name}
            </button>
          ))}
          {trunks.length > shown.length && <em>+{trunks.length - shown.length} more in the sidebar</em>}
        </div>
      )}
    </div>
  )
}

function CopilotActivity({ events, live }: { events: import('../types').TraceEvent[]; live: boolean }) {
  const trajectory = events.length ? projectTrajectory(events) : null
  const spans = trajectory?.spans ?? []
  if (!spans.length) return null
  return (
    <div className="copilot-activity">
      {spans.map((span) => (
        <div key={span.id} className="copilot-activity-row" data-failed={span.failed ? '1' : '0'}>
          <i className={`ph ${span.lane === 'model' ? 'ph-brain' : span.failed ? 'ph-x-circle' : 'ph-wrench'}`} />
          <strong>{span.label}</strong>
          <span className="ellipsis">{span.detail}</span>
          {span.endMs === null && live && <i className="ph ph-circle-notch spin" />}
        </div>
      ))}
    </div>
  )
}

function ArtifactCard({ artifact, onOpen }: { artifact: Artifact; onOpen: () => void }) {
  const meta = ARTIFACT_LABELS[artifact.kind] ?? { icon: 'ph-cube', title: artifact.kind }
  const summary = artifactSummary(artifact)
  return (
    <button className="artifact-card" onClick={onOpen}>
      <span className="artifact-icon"><i className={`ph ${meta.icon}`} /></span>
      <span className="artifact-copy">
        <span className="v-label">{meta.title}</span>
        <strong>{summary}</strong>
      </span>
      <i className="ph ph-arrow-up-right" />
    </button>
  )
}

function artifactSummary(artifact: Artifact): string {
  const data = record(artifact.data)
  if (artifact.kind === 'target_run') return `${data.harness_name ?? 'Harness'} · ${data.status ?? 'finished'}`
  if (artifact.kind === 'harness_created') return String(data.name ?? 'New harness')
  if (artifact.kind === 'interface') return `${data.harness_name ?? 'Harness'} interface`
  if (artifact.kind === 'run_evidence') {
    const run = record(data.run)
    return run.run_id ? runLabel({ run_id: String(run.run_id), alias: run.alias as string | null }) : 'Run evidence'
  }
  if (artifact.kind === 'recent_runs') return `${data.count ?? 0} runs · ${data.harness_name ?? 'Harness'}`
  if (artifact.kind === 'memories') return `${data.count ?? 0} durable memories`
  if (artifact.kind === 'memory_saved') return String(data.content ?? 'Saved for later conversations')
  if (artifact.kind === 'harness_stats') return `${data.total_runs ?? 0} recorded runs`
  if (artifact.kind === 'research_program') {
    const program = record(data.program)
    if (!program.name) return 'No research programs yet'
    const waiting = record(program).contract_to_approve
      ? ' · contract to approve'
      : Array.isArray(program.open_questions) && program.open_questions.length
        ? ` · ${program.open_questions.length} question(s) for you`
        : ''
    return `${program.name} · ${program.status}${waiting}`
  }
  if (artifact.kind === 'improvement_proposal') return data.changed ? 'Draft ready for review' : String(data.summary ?? 'No proposal')
  return String(data.name ?? data.harness_name ?? 'Open details')
}

function record(value: unknown): Record<string, any> {
  return value && typeof value === 'object' ? (value as Record<string, any>) : {}
}
