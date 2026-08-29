/**
 * The workbench state for a run: its visible input/output and live controls.
 *
 * A follow-up replays the visible conversation into a new run. There is no
 * grouping object between a harness version and its runs.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { api, streamResume, streamRun } from './api'
// Explicit extension: `node --test`'s type stripping resolves relative
// specifiers verbatim, and the tests import this module's neighbours directly.
import { conversationFrom } from './runs.ts'
import {
  HiveloomApiError,
  type Attachment,
  type PendingMessage,
  type RunResult,
  type TraceEvent,
} from './types'

export interface Turn {
  role: 'user' | 'assistant' | 'steer'
  content: string
  /** A marker turn: the harness moved between this turn and the last. */
  versionBoundary?: { from: string; to: string }
  /** Trace events for an assistant turn, in arrival order. */
  events?: TraceEvent[]
  result?: RunResult
  error?: string
  runId?: string
  /** Files written into the workspace for this turn, named in its message. */
  attachments?: Attachment[]
  /** Replayed from the journal rather than watched live — no event stream. */
  restored?: boolean
}

export interface RunWorkspace {
  turns: Turn[]
  /** The harness version used for the next invocation. */
  version: string | null
  /**
   * `provider/model-id` the next run uses, or '' for the harness's own.
   * Workbench-scoped on purpose: changing one variable and re-running is the
   * whole point, and doing it by rewriting the spec would change every other
   * conversation too.
   */
  model: string
  setModel: (selector: string) => void
  busy: boolean
  /** Set from the stream's opening frame, so stop and steer work immediately. */
  liveRunId: string | null
  /** The most recent run produced — what Trajectory follows. */
  lastRunId: string | null
  trustNeeded: boolean
  steered: string[]
  /** Queued but not yet delivered — editable until the loop consumes them. */
  queued: PendingMessage[]
  send: (text: string, attachments?: Attachment[]) => Promise<void>
  /** Continue a fork from the exact model-call boundary recorded in fork.yaml. */
  resumeFork: () => Promise<void>
  /** Open a new thread carrying a recorded run's history up to a boundary. */
  branch: (
    messages: { role: string; content: string }[],
    model: string,
    version?: string,
  ) => void
  stop: () => Promise<void>
  steer: (text: string) => Promise<void>
  editQueued: (messageId: string, content: string) => Promise<void>
  dropQueued: (messageId: string) => Promise<void>
  switchModel: (body: { model?: string; provider?: string }) => Promise<void>
  clear: () => void
}

/**
 * @param harnessId  the folder a run executes from
 * @param versionHash  the version a run started now would record.
 * @param currentVersion  re-read at send time. The harness can move without
 *   this tab doing anything — a CLI edit, another agent, an applied proposal
 *   elsewhere — and the boundary has to be drawn on what the run will actually
 *   record, not on what the browser last happened to fetch.
 */
export function useRun(
  harnessId: string | null,
  versionHash?: string,
  currentVersion?: () => Promise<string | undefined>,
): RunWorkspace {
  const [turns, setTurns] = useState<Turn[]>([])
  const [busy, setBusy] = useState(false)
  const [liveRunId, setLiveRunId] = useState<string | null>(null)
  const [lastRunId, setLastRunId] = useState<string | null>(null)
  const [trustNeeded, setTrustNeeded] = useState(false)
  const [steered, setSteered] = useState<string[]>([])
  const [queued, setQueued] = useState<PendingMessage[]>([])
  const abort = useRef<AbortController | null>(null)
  const [version, setVersion] = useState<string | null>(null)
  const [model, setModelState] = useState('')

  // The queue drains as the loop reaches turn boundaries, so it is polled
  // rather than tracked locally: what matters is what is *still* pending.
  const refreshQueue = useCallback(async () => {
    if (!liveRunId) {
      setQueued([])
      return
    }
    try {
      setQueued(await api.pendingMessages(liveRunId))
    } catch {
      setQueued([])
    }
  }, [liveRunId])

  useEffect(() => {
    if (!liveRunId) {
      setQueued([])
      return
    }
    const timer = setInterval(() => void refreshQueue(), 1500)
    return () => clearInterval(timer)
  }, [liveRunId, refreshQueue])

  const editQueued = useCallback(
    async (messageId: string, content: string) => {
      if (!liveRunId) return
      await api.editMessage(liveRunId, messageId, content)
      await refreshQueue()
    },
    [liveRunId, refreshQueue],
  )

  const dropQueued = useCallback(
    async (messageId: string) => {
      if (!liveRunId) return
      await api.dropMessage(liveRunId, messageId)
      await refreshQueue()
    },
    [liveRunId, refreshQueue],
  )

  /** Takes effect at the next turn boundary, and is journalled as a model_swap. */
  const switchModel = useCallback(
    async (body: { model?: string; provider?: string }) => {
      if (!liveRunId) return
      await api.switchModel(liveRunId, { ...body, reason: 'switched from the workbench' })
    },
    [liveRunId],
  )

  const clear = useCallback(() => {
    setVersion(null)
    setTurns([])
    setLiveRunId(null)
    setLastRunId(null)
    setTrustNeeded(false)
    setSteered([])
  }, [])

  /**
   * Continue a recorded run from one of its model calls, in a new run.
   *
   * The messages come from the same fold `hiveloom trace --materialize` and
   * `hiveloom fork` use, so what is replayed is the request the model actually
   * received at that boundary rather than a transcript reassembled here. The
   * harness is not copied and not changed: this is a *conversation* that picks
   * up where a run was, which is why the one variable it offers is the model.
   */
  const branch = useCallback(
    (messages: { role: string; content: string }[], model: string, version?: string) => {
      setTurns(
        messages
          .filter((message) => message.content.trim())
          .map((message) => ({
            role: message.role === 'assistant' ? ('assistant' as const) : ('user' as const),
            content: message.content,
            restored: true,
          })),
      )
      setModelState(model)
      setVersion(version ?? null)
      setLiveRunId(null)
      setLastRunId(null)
      setTrustNeeded(false)
      setSteered([])
      setBusy(false)
    },
    [],
  )

  const send = useCallback(
    async (text: string, attachments?: Attachment[]) => {
      if (!harnessId || !text.trim() || busy) return

      // An attachment is already in the harness's workspace by the time it
      // gets here; what the turn carries is the path. Naming it in the message
      // is what makes it reachable — the harness opens it with the file tool
      // it already has — and it keeps the journal honest: the recorded message
      // says exactly what the model was told.
      const content = attachments?.length
        ? `${text}\n\nAttached in the workspace:\n${attachments
            .map((file) => `- ${file.path} (${formatBytes(file.bytes)})`)
            .join('\n')}`
        : text

      // The whole thread is replayed every turn: run_harness seeds everything
      // before the last user message as history. A steering message goes back
      // as an ordinary user turn — it *was* one, delivered mid-run — because
      // dropping it would lose the instruction from the next turn onward.
      const history = [...turns, { role: 'user' as const, content, attachments }]
      const messages = conversationFrom(history)

      // Mark the point where a follow-up begins executing a newer harness.
      const at = (await currentVersion?.().catch(() => undefined)) ?? versionHash
      const crossed = Boolean(version && at && version !== at)
      if (at) setVersion(at)

      setTurns([
        ...history,
        ...(crossed
          ? [
              {
                role: 'assistant' as const,
                content: '',
                versionBoundary: { from: version as string, to: at as string },
              },
            ]
          : []),
        { role: 'assistant' as const, content: '', events: [] },
      ])
      setBusy(true)
      setTrustNeeded(false)
      setSteered([])

      const controller = new AbortController()
      abort.current = controller

      const pushEvent = (event: TraceEvent) =>
        setTurns((current) => {
          const next = [...current]
          const last = next[next.length - 1]
          next[next.length - 1] = { ...last, events: [...(last.events ?? []), event] }
          return next
        })

      try {
        const outcome = await streamRun(
          harnessId,
          { messages, model: model || undefined },
          pushEvent,
          controller.signal,
          (runId) => {
            setLiveRunId(runId)
            setLastRunId(runId)
            setTurns((current) => {
              const next = [...current]
              next[next.length - 1] = { ...next[next.length - 1], runId }
              return next
            })
          },
        )
        setTurns((current) => {
          const next = [...current]
          const last = next[next.length - 1]
          next[next.length - 1] =
            outcome.type === 'error'
              ? { ...last, error: outcome.error }
              : { ...last, content: outcome.output, result: outcome }
          return next
        })
        if (outcome.type !== 'error') setLastRunId(outcome.run_id)
      } catch (exc) {
        if (exc instanceof HiveloomApiError && exc.needsTrust) setTrustNeeded(true)
        setTurns((current) => {
          const next = [...current]
          next[next.length - 1] = { ...next[next.length - 1], error: String(exc) }
          return next
        })
      } finally {
        setBusy(false)
        setLiveRunId(null)
        abort.current = null
      }
    },
    [busy, currentVersion, harnessId, model, turns, version, versionHash],
  )

  const resumeFork = useCallback(async () => {
    if (!harnessId || busy) return
    setTurns([{ role: 'assistant', content: '', events: [] }])
    setBusy(true)
    setTrustNeeded(false)
    setSteered([])
    if (versionHash) setVersion(versionHash)

    const controller = new AbortController()
    abort.current = controller
    const pushEvent = (event: TraceEvent) =>
      setTurns((current) => {
        const next = [...current]
        const last = next[next.length - 1]
        next[next.length - 1] = { ...last, events: [...(last.events ?? []), event] }
        return next
      })

    try {
      const outcome = await streamResume(
        harnessId,
        {},
        pushEvent,
        controller.signal,
        (runId) => {
          setLiveRunId(runId)
          setLastRunId(runId)
          setTurns((current) => [{ ...current[0], runId }])
        },
      )
      setTurns((current) => {
        const last = current[0]
        return [
          outcome.type === 'error'
            ? { ...last, error: outcome.error }
            : { ...last, content: outcome.output, result: outcome },
        ]
      })
      if (outcome.type !== 'error') setLastRunId(outcome.run_id)
    } catch (exc) {
      if (exc instanceof HiveloomApiError && exc.needsTrust) setTrustNeeded(true)
      setTurns((current) => [{ ...current[0], error: String(exc) }])
    } finally {
      setBusy(false)
      setLiveRunId(null)
      abort.current = null
    }
  }, [busy, harnessId, versionHash])

  /**
   * Graceful by default: the run stops at its next turn boundary and lands as
   * `stopped` with an intact journal. Aborting the fetch would only drop the
   * stream — the run would keep going — so that is the fallback, not the move.
   */
  const stop = useCallback(async () => {
    if (liveRunId) {
      try {
        await api.stop(liveRunId, 'stopped from the workbench')
        return
      } catch {
        // The run is no longer registered here; fall through to the abort.
      }
    }
    abort.current?.abort()
  }, [liveRunId])

  const steer = useCallback(
    async (text: string) => {
      if (!liveRunId || !text.trim()) return
      await api.steer(liveRunId, text.trim())
      setSteered((current) => [...current, text.trim()])
      // Shown in the thread, above the turn it is steering, so the reply can
      // be read against everything the run was actually told.
      setTurns((current) => {
        const next = [...current]
        const at = Math.max(0, next.length - 1)
        next.splice(at, 0, { role: 'steer', content: text.trim() })
        return next
      })
      await refreshQueue()
    },
    [liveRunId, refreshQueue],
  )

  return {
    turns,
    version,
    model,
    setModel: setModelState,
    busy,
    liveRunId,
    lastRunId,
    trustNeeded,
    steered,
    queued,
    send,
    resumeFork,
    branch,
    stop,
    steer,
    editQueued,
    dropQueued,
    switchModel,
    clear,
  }
}

function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${(size / (1024 * 1024)).toFixed(1)} MB`
}
