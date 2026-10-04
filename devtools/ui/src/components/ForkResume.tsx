/**
 * Resume a fork from where it was forked, and show how it went.
 *
 * A fork exists to be resumed: the parent's conversation up to the chosen turn
 * is replayed verbatim and the run continues on the fork's (possibly edited)
 * harness. Running it from the Use tab instead would start a brand-new run and
 * throw that context away.
 */
import { useRef, useState } from 'react'
import { streamResume } from '../api'
import type { RunResult } from '../types'
import { Notice, StatusPill } from './common'

export function ForkResume({
  harnessId,
  turn,
  onFinished,
  compact = false,
}: {
  harnessId: string
  turn: number
  /** Called with the resumed run's id once it has finished (or failed). */
  onFinished?: (runId: string | null) => void
  compact?: boolean
}) {
  const [state, setState] = useState<'idle' | 'running' | 'done'>('idle')
  const [calls, setCalls] = useState(0)
  const [outcome, setOutcome] = useState<RunResult | null>(null)
  const [error, setError] = useState<string | null>(null)
  const runId = useRef<string | null>(null)
  const abort = useRef<AbortController | null>(null)

  const start = async () => {
    setState('running')
    setCalls(0)
    setOutcome(null)
    setError(null)
    runId.current = null
    abort.current = new AbortController()
    try {
      const result = await streamResume(
        harnessId,
        (event) => {
          if (event.type === 'model_call') setCalls((count) => count + 1)
        },
        abort.current.signal,
        (id) => {
          runId.current = id
        },
      )
      if (result.type === 'error') setError(result.error)
      else setOutcome(result)
    } catch (exc) {
      setError(String(exc))
    } finally {
      setState('done')
      onFinished?.(runId.current)
    }
  }

  return (
    <div className="fork-resume" data-compact={compact ? '1' : '0'}>
      <button
        className={compact ? 'v-btn v-btn-sm' : 'v-btn v-btn-primary'}
        disabled={state === 'running'}
        onClick={() => void start()}
        title={`Replay the parent's conversation up to turn ${turn} and continue on this fork`}
      >
        {state === 'running' ? <i className="ph ph-circle-notch spin" /> : <i className="ph ph-play" />}
        {state === 'running'
          ? `Resuming… ${calls ? `${calls} model call${calls === 1 ? '' : 's'}` : ''}`
          : state === 'done'
            ? `Resume again from turn ${turn}`
            : `Resume from turn ${turn}`}
      </button>
      {outcome && (
        <div className="fork-resume-outcome">
          <StatusPill status={outcome.status} />
          <span className="mono">
            {outcome.turns} turn{outcome.turns === 1 ? '' : 's'} · ${outcome.cost_usd.toFixed(4)}
          </span>
          {!compact && outcome.output && <pre>{outcome.output}</pre>}
          {!compact &&
            outcome.verdicts
              .filter((verdict) => !verdict.passed && verdict.feedback)
              .map((verdict) => (
                <span key={verdict.verifier} className="fork-resume-feedback">
                  {verdict.verifier}: {verdict.feedback}
                </span>
              ))}
        </div>
      )}
      {error && <Notice icon="ph-warning-octagon" tone="err" title="Resume failed" body={error} />}
    </div>
  )
}
