import { useEffect, useRef, useState } from 'react'
import { api, streamRun } from '../api'
import { runLabel } from '../runs'
import type { Artifact, RunResult, TraceEvent, Verdict } from '../types'
import { Stat, StatRow, StatusPill, VerdictChip } from './common'

export function CopilotCanvas({
  artifact,
  onClose,
  onChanged,
}: {
  artifact: Artifact
  onClose: () => void
  onChanged: () => Promise<void>
}) {
  const data = record(artifact.data)
  return (
    <aside className="copilot-canvas rise">
      <header>
        <div>
          <div className="v-label">Active artifact</div>
          <h2>{artifactTitle(artifact.kind)}</h2>
        </div>
        <button className="icon-btn" onClick={onClose} title="Close artifact">
          <i className="ph ph-x" />
        </button>
      </header>
      <div className="copilot-canvas-body">
        {artifact.kind === 'interface' ? (
          <InterfacePreview data={data} onChanged={onChanged} />
        ) : artifact.kind === 'target_run' ? (
          <TargetRun data={data} />
        ) : artifact.kind === 'run_evidence' || artifact.kind === 'run_detail' ? (
          <RunEvidence data={data} />
        ) : artifact.kind === 'recent_runs' ? (
          <RecentRuns data={data} />
        ) : artifact.kind === 'harness_stats' ? (
          <HarnessStats data={data} />
        ) : artifact.kind === 'version_comparison' ? (
          <VersionComparison data={data} />
        ) : artifact.kind === 'improvement_proposal' ? (
          <Proposal data={data} onChanged={onChanged} />
        ) : artifact.kind === 'harness_contract' || artifact.kind === 'harness_created' ? (
          <HarnessContract data={data} />
        ) : (
          <GenericArtifact data={data} />
        )}
      </div>
    </aside>
  )
}

export function InterfacePreview({
  data,
  onChanged,
  onOpenRun,
}: {
  data: Record<string, unknown>
  onChanged: () => Promise<void>
  onOpenRun?: (runId: string) => void
}) {
  const frame = useRef<HTMLIFrameElement | null>(null)
  const abort = useRef<AbortController | null>(null)
  const runningRef = useRef(false)
  const [run, setRun] = useState<RunResult | null>(null)
  const [running, setRunning] = useState(false)
  const [liveRunId, setLiveRunId] = useState<string | null>(null)
  const [progress, setProgress] = useState('')
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const receive = async (event: MessageEvent) => {
      if (event.source !== frame.current?.contentWindow) return
      const message = record(event.data)
      if (message.type !== 'hiveloom:run') return
      const requestId = String(message.requestId ?? '')
      const harnessId = String(data.harness_id ?? '')
      if (!requestId || !harnessId) return
      if (String(message.harnessId ?? '') !== harnessId) {
        frame.current?.contentWindow?.postMessage(
          { type: 'hiveloom:result', requestId, error: 'The preview may only run its own harness.' },
          '*',
        )
        return
      }
      if (runningRef.current) {
        frame.current?.contentWindow?.postMessage(
          { type: 'hiveloom:result', requestId, error: 'A preview run is already in progress.' },
          '*',
        )
        return
      }
      runningRef.current = true
      setRunning(true)
      setRun(null)
      setError(null)
      setProgress('Preparing the harness…')
      const controller = new AbortController()
      abort.current = controller
      try {
        let input = String(message.input ?? '')
        const file = record(message.file)
        if (file.name && file.contentBase64) {
          const binary = atob(String(file.contentBase64))
          const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0))
          const attachment = await api.upload(
            harnessId,
            new File([bytes], String(file.name), { type: String(file.type ?? '') }),
          )
          input = attachment.path
        }
        const announce = (event: TraceEvent) => {
          const next = interfaceProgress(event)
          if (!next) return
          setProgress(next)
          frame.current?.contentWindow?.postMessage(
            { type: 'hiveloom:progress', requestId, status: next },
            '*',
          )
        }
        const outcome = await streamRun(
          harnessId,
          { input },
          announce,
          controller.signal,
          (runId) => {
            setLiveRunId(runId)
            setProgress('Harness started…')
          },
        )
        if (outcome.type === 'error') throw new Error(outcome.error)
        setRun(outcome)
        setProgress('Finished')
        frame.current?.contentWindow?.postMessage(
          {
            type: 'hiveloom:result',
            requestId,
            result: outcome.output,
            meta: {
              runId: outcome.run_id,
              status: outcome.status,
              turns: outcome.turns,
              costUsd: outcome.cost_usd,
              verdicts: outcome.verdicts,
            },
          },
          '*',
        )
        try { await onChanged() } catch { /* The completed run remains valid and visible. */ }
      } catch (error) {
        const detail = error instanceof DOMException && error.name === 'AbortError'
          ? 'The preview run was stopped.'
          : String(error)
        setError(detail)
        frame.current?.contentWindow?.postMessage(
          { type: 'hiveloom:result', requestId, error: detail },
          '*',
        )
      } finally {
        runningRef.current = false
        setRunning(false)
        setLiveRunId(null)
        abort.current = null
      }
    }
    window.addEventListener('message', receive)
    return () => window.removeEventListener('message', receive)
  }, [data.harness_id, onChanged])

  return (
    <div className="interface-preview">
      <div className="interface-use-head">
        <div>
          <div className="v-label">Use harness</div>
          <strong>{String(data.harness_name ?? data.harness_id ?? 'Harness')}</strong>
          <p>Submit the interface below to execute a real, recorded harness run.</p>
        </div>
        <span className="interface-ready" data-running={running ? '1' : '0'}>
          <span className="dot" /> {running ? 'Running' : 'Ready'}
        </span>
      </div>
      <div className="artifact-summary-line">
        <span className="mono">{interfaceDisplayPath(data.path)}</span>
        <span className="rail-tag">sandboxed</span>
      </div>
      <iframe
        ref={frame}
        title={`${String(data.harness_name ?? 'Harness')} interface preview`}
        sandbox="allow-scripts"
        srcDoc={String(data.html ?? '')}
      />
      {run && (
        <div className="interface-run-meta">
          <StatusPill status={run.status} />
          <span title={run.run_id}>{runLabel(run)}</span>
          <span>{run.turns} turns</span>
          <span>${run.cost_usd.toFixed(4)}</span>
          {(run.verdicts ?? []).map((verdict) => (
            <VerdictChip
              key={verdict.verifier}
              name={verdict.verifier}
              passed={Boolean(verdict.passed)}
              title={verdict.feedback}
            />
          ))}
          {onOpenRun && (
            <button className="v-btn v-btn-ghost v-btn-sm" onClick={() => onOpenRun(run.run_id)}>
              <i className="ph ph-path" /> Open trace
            </button>
          )}
        </div>
      )}
      {running && (
        <div className="interface-run-meta live">
          <i className="ph ph-circle-notch spin" />
          <span>{progress || 'Running…'}</span>
          {liveRunId && <span className="mono">{liveRunId}</span>}
          <button
            className="v-btn v-btn-ghost v-btn-sm"
            onClick={() => {
              if (liveRunId) void api.stop(liveRunId, 'stopped from interface preview')
              else abort.current?.abort()
            }}
          >
            <i className="ph ph-stop" /> Stop
          </button>
        </div>
      )}
      {error && <div className="copilot-error">{error}</div>}
      <p className="canvas-note">
        The preview receives no credentials or workbench access. Its narrow message bridge can
        only execute the harness named in the artifact.
      </p>
    </div>
  )
}

function interfaceDisplayPath(value: unknown): string {
  const path = String(value ?? '').replaceAll('\\', '/')
  const interfaceAt = path.lastIndexOf('/interfaces/')
  if (interfaceAt >= 0) return path.slice(interfaceAt + 1)
  if (!path || path.startsWith('/')) return 'standalone HTML'
  return path
}

function interfaceProgress(event: TraceEvent): string {
  const payload = record(event.payload)
  if (event.type === 'model_call') return `Calling the model${payload.turn ? ` · turn ${payload.turn}` : ''}…`
  if (event.type === 'tool_call') {
    return `Running ${String(payload.tool_name ?? payload.tool ?? payload.name ?? 'a tool')}…`
  }
  if (event.type === 'verification_started') return 'Verifying the result…'
  if (event.type === 'verification_result') {
    return payload.passed ? 'Verification passed.' : 'Verification requested another attempt…'
  }
  if (event.type === 'guardrail_triggered') return 'A guardrail stopped the run.'
  return ''
}

function TargetRun({ data }: { data: Record<string, unknown> }) {
  const verdicts = array(data.verdicts) as unknown as Verdict[]
  return (
    <div className="canvas-stack">
      <div className="canvas-title-row">
        <StatusPill status={String(data.status ?? 'unknown')} />
        <span title={String(data.run_id ?? '')}>
          {runLabel({ run_id: String(data.run_id ?? ''), alias: data.alias as string | null })}
        </span>
      </div>
      <StatRow>
        <Stat label="Turns" value={String(data.turns ?? 0)} />
        <Stat label="Cost" value={`$${number(data.cost_usd).toFixed(4)}`} />
        <Stat label="Duration" value={`${number(data.duration_seconds).toFixed(1)}s`} />
      </StatRow>
      {verdicts.length > 0 && (
        <section>
          <div className="v-label">Verification</div>
          <div className="canvas-chip-row">
            {verdicts.map((verdict) => (
              <VerdictChip
                key={verdict.verifier}
                name={verdict.verifier}
                passed={Boolean(verdict.passed)}
                title={verdict.feedback}
              />
            ))}
          </div>
        </section>
      )}
      <section>
        <div className="v-label">Output</div>
        <pre className="canvas-output">{pretty(data.output)}</pre>
      </section>
    </div>
  )
}

function RunEvidence({ data }: { data: Record<string, unknown> }) {
  const run = record(data.run)
  const evidence = array(data.evidence)
  const events = array(data.events)
  return (
    <div className="canvas-stack">
      <div className="canvas-title-row">
        <StatusPill status={String(run.status ?? 'unknown')} />
        <span title={String(run.run_id ?? '')}>
          {runLabel({ run_id: String(run.run_id ?? ''), alias: run.alias as string | null })}
        </span>
      </div>
      <p className="canvas-lede">{String(run.task ?? 'No task statement recorded.')}</p>
      <StatRow>
        <Stat label="Turns" value={String(run.turns ?? 0)} />
        <Stat label="Cost" value={`$${number(run.cost_usd).toFixed(4)}`} />
        <Stat label="Events" value={String(data.event_count ?? events.length)} />
      </StatRow>
      <section>
        <div className="v-label">Actionable evidence</div>
        {evidence.length ? (
          <div className="evidence-list">
            {evidence.map((item, index) => {
              const row = record(item)
              return (
                <div key={index}>
                  <i className="ph ph-warning-circle" />
                  <div><strong>{String(row.name ?? row.type ?? 'Evidence')}</strong><p>{String(row.detail ?? '')}</p></div>
                </div>
              )
            })}
          </div>
        ) : (
          <p className="canvas-note">No failed verifier, guardrail, or tool-error event was recorded.</p>
        )}
      </section>
      {Boolean(data.integrity) && <p className="canvas-note">Journal: {String(data.integrity)}</p>}
    </div>
  )
}

function HarnessStats({ data }: { data: Record<string, unknown> }) {
  return (
    <div className="canvas-stack">
      <h3>{String(data.harness_name ?? 'Harness')}</h3>
      <StatRow>
        <Stat label="Runs" value={String(data.total_runs ?? 0)} />
        <Stat label="Success" value={`${Math.round(number(data.success_rate) * 100)}%`} color="var(--ok)" />
        <Stat label="Avg cost" value={`$${number(data.avg_cost_usd).toFixed(4)}`} />
        <Stat label="Avg turns" value={number(data.avg_turns).toFixed(1)} />
      </StatRow>
      <section>
        <div className="v-label">Versions</div>
        <pre className="canvas-output">{pretty(data.versions)}</pre>
      </section>
      <section>
        <div className="v-label">Recurring failures</div>
        <pre className="canvas-output">{pretty(data.failure_signatures)}</pre>
      </section>
    </div>
  )
}

function RecentRuns({ data }: { data: Record<string, unknown> }) {
  const runs = array(data.runs)
  return (
    <div className="canvas-stack">
      <div>
        <div className="v-label">Harness</div>
        <h3>{String(data.harness_name ?? 'Harness')}</h3>
      </div>
      {runs.length ? (
        <div className="recent-run-list">
          {runs.map((value, index) => {
            const run = record(value)
            return (
              <div key={String(run.run_id ?? index)}>
                <StatusPill status={String(run.status ?? 'unknown')} />
                <div>
                  <strong title={String(run.run_id ?? '')}>
                    {runLabel({ run_id: String(run.run_id ?? ''), alias: run.alias as string | null })}
                  </strong>
                  <small className="ellipsis">{String(run.task ?? 'No task statement')}</small>
                </div>
                <small>{String(run.turns ?? 0)} turns · ${number(run.cost_usd).toFixed(4)}</small>
              </div>
            )
          })}
        </div>
      ) : <p className="canvas-note">No recorded runs for this harness.</p>}
    </div>
  )
}

function VersionComparison({ data }: { data: Record<string, unknown> }) {
  const delta = record(data.delta)
  return (
    <div className="canvas-stack">
      <h3>{String(data.harness_name ?? 'Harness')} comparison</h3>
      {Boolean(data.underpowered) && (
        <div className="canvas-warning"><i className="ph ph-warning" /> Fewer than five runs exist on at least one side; treat this as directional.</div>
      )}
      <StatRow>
        <Stat label="Success delta" value={`${(number(delta.success_rate) * 100).toFixed(0)} pts`} />
        <Stat label="Cost delta" value={`$${number(delta.avg_cost_usd).toFixed(4)}`} />
        <Stat label="Turn delta" value={number(delta.avg_turns).toFixed(1)} />
      </StatRow>
      <ComparisonList label="Fixed failures" values={array(data.fixed_failures)} tone="ok" />
      <ComparisonList label="New failures" values={array(data.new_failures)} tone="err" />
    </div>
  )
}

function Proposal({ data, onChanged }: { data: Record<string, unknown>; onChanged: () => Promise<void> }) {
  const [status, setStatus] = useState(String(data.status ?? (data.changed ? 'pending' : 'none')))
  const [error, setError] = useState<string | null>(null)
  const gate = record(data.gate)
  const proposal = record(data.proposal)
  const apply = async () => {
    setError(null)
    try {
      await api.applyProposal(String(data.harness_id), String(data.id), [], [])
      setStatus('applied')
      await onChanged()
    } catch (exc) {
      setError(String(exc))
    }
  }
  if (!data.changed) return <p className="canvas-lede">{String(data.summary ?? 'No proposal was created.')}</p>
  return (
    <div className="canvas-stack">
      <div className="canvas-title-row"><span className="proposal-status">{status}</span><span className="mono">{String(data.id ?? '')}</span></div>
      <p className="canvas-lede">{String(data.rationale ?? proposal.rationale ?? '')}</p>
      <section><div className="v-label">Accepted YAML changes</div><pre className="canvas-output">{pretty(gate.accepted)}</pre></section>
      {array(gate.rejected).length > 0 && <section><div className="v-label">Refused by safety gate</div><pre className="canvas-output">{pretty(gate.rejected)}</pre></section>}
      {status === 'pending' && (
        <button className="v-btn v-btn-primary" onClick={() => void apply()}>
          <i className="ph ph-check" /> Apply YAML changes
        </button>
      )}
      {array(gate.code_changes).length > 0 && <p className="canvas-warning">Code changes remain unapproved. This action applies YAML only.</p>}
      {array(gate.prose_changes).length > 0 && <p className="canvas-warning">Prompt prose changes remain unapproved. This action applies YAML only.</p>}
      {error && <div className="copilot-error">{error}</div>}
    </div>
  )
}

function HarnessContract({ data }: { data: Record<string, unknown> }) {
  const contract = record(data.input_contract)
  return (
    <div className="canvas-stack">
      <div><div className="v-label">Harness</div><h3>{String(data.name ?? '')}</h3><p className="canvas-lede">{String(data.description ?? '')}</p></div>
      <div className="contract-grid">
        <div><span>Input</span><strong>{String(contract.label ?? contract.kind ?? 'Task')}</strong></div>
        <div><span>Version</span><strong className="mono">{String(data.version_hash ?? '').slice(0, 8)}</strong></div>
        <div><span>Tools</span><strong>{array(data.tools).length}</strong></div>
        <div><span>Guardrails</span><strong>{array(data.guardrails).length}</strong></div>
      </div>
      <section><div className="v-label">Verification</div><pre className="canvas-output">{pretty(data.verification)}</pre></section>
    </div>
  )
}

function ComparisonList({ label, values, tone }: { label: string; values: unknown[]; tone: string }) {
  return <section><div className="v-label">{label}</div>{values.length ? <ul className={`comparison-list ${tone}`}>{values.map((value, index) => <li key={index}>{String(value)}</li>)}</ul> : <p className="canvas-note">None recorded.</p>}</section>
}

function GenericArtifact({ data }: { data: Record<string, unknown> }) {
  return <pre className="canvas-output">{pretty(data)}</pre>
}

function artifactTitle(kind: string): string {
  return ({
    interface: 'Interface preview', target_run: 'Target run', recent_runs: 'Recent runs', run_evidence: 'Run evidence',
    run_detail: 'Run evidence', harness_stats: 'Harness results', version_comparison: 'Version comparison',
    improvement_proposal: 'Improvement proposal', harness_contract: 'Harness contract',
    harness_created: 'Harness created',
  } as Record<string, string>)[kind] ?? kind.replaceAll('_', ' ')
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {}
}
function array(value: unknown): unknown[] { return Array.isArray(value) ? value : [] }
function number(value: unknown): number { const parsed = Number(value); return Number.isFinite(parsed) ? parsed : 0 }
function pretty(value: unknown): string {
  if (typeof value === 'string') { try { return JSON.stringify(JSON.parse(value), null, 2) } catch { return value } }
  return JSON.stringify(value ?? null, null, 2)
}
