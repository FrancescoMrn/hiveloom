import { useCallback, useEffect, useRef, useState } from 'react'
import { api, streamCopilot } from './api'
import type {
  Artifact,
  ConversationMessage,
  ConversationSelection,
  TraceEvent,
} from './types'

export type CopilotMessage = ConversationMessage

export type CopilotSelection = ConversationSelection

export interface CopilotWorkspace {
  messages: CopilotMessage[]
  busy: boolean
  liveRunId: string | null
  send: (text: string, attachments?: import('./types').Attachment[]) => Promise<void>
  stop: () => Promise<void>
  clear: () => void
}

/**
 * One conversation with the bundled Hiveloom expert.
 *
 * Each turn is an ordinary Hiveloom run with the visible conversation replayed
 * as history. The selected target is caller-owned context, not prose hidden in
 * the thread; the expert reads it through its `workspace_context` tool.
 */
export function useCopilot(
  conversationId: string | null,
  initialMessages: CopilotMessage[],
  selection: CopilotSelection,
  model: string,
  onFinished?: (artifacts: Artifact[]) => void,
  onPersisted?: () => void,
): CopilotWorkspace {
  const [messages, setMessages] = useState<CopilotMessage[]>(initialMessages)
  const [busy, setBusy] = useState(false)
  const [liveRunId, setLiveRunId] = useState<string | null>(null)
  const abort = useRef<AbortController | null>(null)

  useEffect(() => {
    setMessages(initialMessages)
    setLiveRunId(null)
  }, [conversationId]) // The record is loaded once per selected conversation.

  const send = useCallback(
    async (value: string, attachments: import('./types').Attachment[] = []) => {
      const text = value.trim()
      if (!text || busy || !conversationId) return
      const userMessage: CopilotMessage = {
        role: 'user',
        content: text,
        ...(attachments.length ? { attachments } : {}),
      }
      const conversation = [
        ...messages
          .filter((message) => message.content.trim())
          .map((message) => ({ role: message.role, content: contentForModel(message) })),
        { role: 'user' as const, content: contentForModel(userMessage) },
      ]
      const started: CopilotMessage[] = [
        ...messages,
        userMessage,
        { role: 'assistant', content: '', events: [], artifacts: [] },
      ]
      setMessages(started)
      setBusy(true)
      try {
        await api.saveConversation(conversationId, { messages: started, selection })
        onPersisted?.()
      } catch (error) {
        const failed = replaceLast(started, { error: `Could not persist conversation: ${String(error)}` })
        setMessages(failed)
        setBusy(false)
        return
      }
      const controller = new AbortController()
      abort.current = controller
      const liveEvents: TraceEvent[] = []

      const pushEvent = (event: TraceEvent) => {
        liveEvents.push(event)
        setMessages((current) => {
          const next = [...current]
          const last = next[next.length - 1]
          next[next.length - 1] = { ...last, events: [...(last.events ?? []), event] }
          return next
        })
      }

      try {
        const outcome = await streamCopilot(
          { messages: conversation, selection, ...(model ? { model } : {}) },
          pushEvent,
          controller.signal,
          setLiveRunId,
        )
        if (outcome.type === 'error') {
          const finished = replaceLast(started, { error: outcome.error, events: liveEvents })
          setMessages(finished)
          await api.saveConversation(conversationId, { messages: finished, selection })
        } else {
          const artifacts = outcome.artifacts ?? []
          const finished = replaceLast(started, {
            content: outcome.output,
            result: outcome,
            artifacts,
            events: liveEvents,
          })
          setMessages(finished)
          await api.saveConversation(conversationId, { messages: finished, selection })
          onFinished?.(artifacts)
        }
        onPersisted?.()
      } catch (error) {
        const finished = replaceLast(started, { error: String(error), events: liveEvents })
        setMessages(finished)
        try {
          await api.saveConversation(conversationId, { messages: finished, selection })
          onPersisted?.()
        } catch {
          // The visible error remains useful even if persistence also failed.
        }
      } finally {
        setBusy(false)
        setLiveRunId(null)
        abort.current = null
      }
    },
    [busy, conversationId, messages, model, onFinished, onPersisted, selection],
  )

  const stop = useCallback(async () => {
    if (liveRunId) {
      try {
        await api.stop(liveRunId, 'stopped from the copilot conversation')
        return
      } catch {
        // If the server already released it, aborting only closes this stream.
      }
    }
    abort.current?.abort()
  }, [liveRunId])

  const clear = useCallback(() => undefined, [])

  return { messages, busy, liveRunId, send, stop, clear }
}

function contentForModel(message: CopilotMessage): string {
  if (message.role !== 'user' || !message.attachments?.length) return message.content
  const files = message.attachments.map(
    (attachment) =>
      `- ${attachment.name}: ${attachment.path} (${attachment.bytes} bytes, sha256 ${attachment.sha256})`,
  )
  return [
    message.content,
    '',
    'Files attached to the selected harness:',
    ...files,
    'Use read_harness_file with the relative path when their contents are relevant.',
  ].join('\n')
}

function replaceLast(
  current: CopilotMessage[],
  patch: Partial<CopilotMessage>,
): CopilotMessage[] {
  const next = [...current]
  const last = next[next.length - 1]
  next[next.length - 1] = { ...last, ...patch }
  return next
}
