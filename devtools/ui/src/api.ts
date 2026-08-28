import {
  HiveloomApiError,
  type ApiError,
  type Attachment,
  type Catalog,
  type CopilotInfo,
  type ConversationMessage,
  type ConversationRecord,
  type ConversationSelection,
  type ConversationSummary,
  type Harness,
  type HarnessDetail,
  type HarnessInterface,
  type ApplyResult,
  type Comparison,
  type ForkResult,
  type MaterializedContext,
  type MemoryRecord,
  type PendingMessage,
  type Proposal,
  type Provider,
  type RunResult,
  type RunDetail,
  type RunRow,
  type TraceEvent,
  type VersionTags,
} from './types'

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { 'content-type': 'application/json', ...(init?.headers ?? {}) },
  })
  if (!response.ok) {
    // The server answers every failure with the same envelope; anything else
    // means the API process is not the thing on the other end (a stale proxy,
    // a wrong port), which is worth saying plainly rather than as "unexpected
    // token < in JSON".
    let info: ApiError
    try {
      info = ((await response.json()) as { error: ApiError }).error
    } catch {
      info = { code: 'unreachable', message: `${response.status} ${response.statusText}` }
    }
    throw new HiveloomApiError(info)
  }
  return (await response.json()) as T
}

export const api = {
  copilot: () => call<CopilotInfo>('/api/copilot'),

  conversations: () =>
    call<{ conversations: ConversationSummary[] }>('/api/conversations').then(
      (result) => result.conversations,
    ),

  createConversation: () =>
    call<ConversationRecord>('/api/conversations', {
      method: 'POST',
      body: JSON.stringify({}),
    }),

  conversation: (id: string) => call<ConversationRecord>(`/api/conversations/${id}`),

  saveConversation: (
    id: string,
    body: {
      messages?: ConversationMessage[]
      selection?: ConversationSelection
      title?: string
    },
  ) =>
    call<ConversationRecord>(`/api/conversations/${id}`, {
      method: 'PUT',
      body: JSON.stringify(body),
    }),

  deleteConversation: (id: string) =>
    call<{ ok: true }>(`/api/conversations/${id}`, { method: 'DELETE' }),

  memories: (harnessId?: string) =>
    call<{ memories: MemoryRecord[] }>(
      `/api/memories${harnessId ? `?harness=${encodeURIComponent(harnessId)}` : ''}`,
    ).then((result) => result.memories),

  remember: (content: string, harnessId?: string) =>
    call<MemoryRecord>('/api/memories', {
      method: 'POST',
      body: JSON.stringify({ content, ...(harnessId ? { harness_id: harnessId } : {}) }),
    }),

  forget: (memoryId: string) =>
    call<{ ok: true }>(`/api/memories/${memoryId}`, { method: 'DELETE' }),

  harnesses: () => call<{ harnesses: Harness[] }>('/api/harnesses').then((r) => r.harnesses),

  harness: (id: string) => call<HarnessDetail>(`/api/harnesses/${id}`),

  harnessInterface: (id: string) =>
    call<HarnessInterface>(`/api/harnesses/${id}/interface`),

  create: (body: { directory: string; name: string; task: string; trust: boolean }) =>
    call<{ id: string; trusted: boolean }>('/api/harnesses', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  saveSpec: (id: string, yaml: string) =>
    call<{ ok: true; id: string }>(`/api/harnesses/${id}/spec`, {
      method: 'PUT',
      body: JSON.stringify({ yaml }),
    }),

  validate: (id: string) =>
    call<{ ok: true; name: string }>(`/api/harnesses/${id}/validate`, { method: 'POST' }),

  trust: (id: string) =>
    call<{ ok: true }>(`/api/harnesses/${id}/trust`, { method: 'POST' }),

  runs: (id: string) => call<{ runs: RunRow[] }>(`/api/harnesses/${id}/runs`).then((r) => r.runs),

  run: (runId: string) => call<RunDetail>(`/api/runs/${runId}`),

  context: (runId: string, seq: number) =>
    call<MaterializedContext>(`/api/runs/${runId}/context/${seq}`),

  catalog: () => call<{ catalog: Catalog }>('/api/catalog').then((r) => r.catalog),

  /**
   * The model directory as *one harness* sees it. Without the id the answer is
   * still correct, only broader: a provider a harness extension registered is
   * reported unavailable, because nothing said which harness is asking.
   */
  providers: (harnessId?: string) =>
    call<{ providers: Provider[] }>(
      harnessId ? `/api/providers?harness=${encodeURIComponent(harnessId)}` : '/api/providers',
    ).then((r) => r.providers),

  /**
   * Provider and id move together — the spec validates them against each
   * other — and the two sampling limits ride along, each its own validated
   * commit. Omitted fields are left alone.
   */
  setModel: (
    id: string,
    body: { selector?: string; temperature?: number | ''; max_input_tokens?: number },
  ) =>
    call<{
      ok: true
      provider: string
      id: string
      temperature: number | null
      max_input_tokens: number
    }>(`/api/harnesses/${id}/model`, { method: 'PUT', body: JSON.stringify(body) }),

  /** The journal file as written — the hash chain is over those bytes. */
  exportUrl: (runId: string) => `/api/runs/${runId}/export`,

  /* ------------------------------------------------- version labels */

  tags: (id: string) =>
    call<{ tags: VersionTags }>(`/api/harnesses/${id}/tags`).then((r) => r.tags),

  /** An empty label clears it; the server keeps the file, not the browser. */
  setTag: (id: string, version: string, label: string) =>
    call<{ ok: true; tags: VersionTags }>(`/api/harnesses/${id}/tags`, {
      method: 'PUT',
      body: JSON.stringify({ version, label }),
    }).then((r) => r.tags),

  /* ---------------------------------------------------- attachments */

  /**
   * Put a file in the harness's workspace and get back the path a turn can
   * name. Base64 rather than multipart so the API keeps its single JSON
   * envelope and adds no dependency.
   */
  upload: async (id: string, file: File): Promise<Attachment> => {
    const bytes = new Uint8Array(await file.arrayBuffer())
    let binary = ''
    // In chunks: spreading a multi-megabyte array into String.fromCharCode
    // overflows the argument list.
    for (let index = 0; index < bytes.length; index += 8192) {
      binary += String.fromCharCode(...bytes.subarray(index, index + 8192))
    }
    const result = await call<{ ok: true; path: string; bytes: number; sha256: string }>(
      `/api/harnesses/${id}/files`,
      {
        method: 'POST',
        body: JSON.stringify({ name: file.name, content_base64: btoa(binary) }),
      },
    )
    return { name: file.name, path: result.path, bytes: result.bytes, sha256: result.sha256 }
  },

  /** Graceful: the run finishes its turn and lands as `stopped`, journal intact. */
  stop: (runId: string, reason?: string) =>
    call<{ ok: true; stopping: true }>(`/api/runs/${runId}/stop`, {
      method: 'POST',
      body: JSON.stringify({ reason }),
    }),

  /** Queue an operator message for the run's next turn boundary. */
  steer: (runId: string, content: string) =>
    call<{ ok: true; id: string }>(`/api/runs/${runId}/messages`, {
      method: 'POST',
      body: JSON.stringify({ content }),
    }),

  /* --------------------------------------------------------- evolution */

  proposals: (id: string, status?: string) =>
    call<{ proposals: Proposal[] }>(
      `/api/harnesses/${id}/proposals${status ? `?status=${status}` : ''}`,
    ).then((r) => r.proposals),

  /**
   * Drafts only — applying is a separate, explicit call.
   *
   * `fromParent` analyses the parent run's version instead of the one on disk:
   * a fork has no runs of its own until it is resumed, so its evidence is the
   * parent's. Only meaningful on a fork; the server refuses it elsewhere.
   */
  propose: (id: string, model?: string, fromParent?: boolean) =>
    call<{ ok: true; changed: boolean; reason?: string } & Partial<Proposal>>(
      `/api/harnesses/${id}/evolve/propose`,
      { method: 'POST', body: JSON.stringify({ model, from_parent: fromParent }) },
    ),

  /** A code change not named in `approve_code` stays pending: silence is refusal. */
  applyProposal: (id: string, proposalId: string, approveCode: string[]) =>
    call<ApplyResult>(`/api/harnesses/${id}/proposals/${proposalId}/apply`, {
      method: 'POST',
      body: JSON.stringify({ approve_code: approveCode, apply_yaml: true }),
    }),

  rejectProposal: (proposalId: string, reason: string) =>
    call<{ ok: true }>(`/api/proposals/${proposalId}/reject`, {
      method: 'POST',
      body: JSON.stringify({ reason }),
    }),

  compare: (id: string, left: string, right: string) =>
    call<Comparison>(
      `/api/harnesses/${id}/compare?left=${encodeURIComponent(left)}&right=${encodeURIComponent(right)}`,
    ),

  /* ------------------------------------------------- live run control */

  pendingMessages: (runId: string) =>
    call<{ messages: PendingMessage[] }>(`/api/runs/${runId}/messages`).then((r) => r.messages),

  editMessage: (runId: string, messageId: string, content: string) =>
    call<{ ok: true }>(`/api/runs/${runId}/messages/${messageId}`, {
      method: 'PATCH',
      body: JSON.stringify({ content }),
    }),

  dropMessage: (runId: string, messageId: string) =>
    call<{ ok: true }>(`/api/runs/${runId}/messages/${messageId}`, { method: 'DELETE' }),

  switchModel: (runId: string, body: { model?: string; provider?: string; reason?: string }) =>
    call<{ ok: true; queued_for_next_turn: true }>(`/api/runs/${runId}/model`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  fork: (
    runId: string,
    body: { at?: number; name?: string; model?: string; provider?: string },
  ) =>
    call<ForkResult>(`/api/runs/${runId}/fork`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
}

/**
 * Run a harness, delivering trace events as they arrive.
 *
 * The response is NDJSON — the same event vocabulary `hiveloom run --stream`
 * prints — so a chunk may split a line and a chunk may carry several. The
 * buffer below is what makes the difference invisible to callers.
 */
export async function streamRun(
  id: string,
  body: ({ input: string } | { messages: { role: string; content: string }[] }) & {
    /**
     * `provider/model-id` for this run only. Queued as an operator model
     * switch the loop consumes before its first call, so the run records a
     * `model_swap` and the harness on disk is left alone.
     */
    model?: string
  },
  onEvent: (event: TraceEvent) => void,
  signal?: AbortSignal,
  /** Called with the run id from the opening `run_accepted` frame, before the
   *  first model call returns — which is what makes stop and steer reachable. */
  onAccepted?: (runId: string) => void,
): Promise<RunResult | { type: 'error'; error: string }> {
  return streamEndpoint(
    `/api/harnesses/${id}/run`,
    body,
    onEvent,
    signal,
    onAccepted,
  )
}

/** Run the bundled Hiveloom expert. Target harnesses are tools of this run. */
export async function streamCopilot(
  body: ({ input: string } | { messages: { role: string; content: string }[] }) & {
    model?: string
    selection?: { harness_id?: string; run_id?: string }
  },
  onEvent: (event: TraceEvent) => void,
  signal?: AbortSignal,
  onAccepted?: (runId: string) => void,
): Promise<RunResult | { type: 'error'; error: string }> {
  return streamEndpoint('/api/copilot/chat', body, onEvent, signal, onAccepted)
}

/** Resume a harness fork from its recorded model-call boundary. */
export async function streamResume(
  id: string,
  body: Record<string, never>,
  onEvent: (event: TraceEvent) => void,
  signal?: AbortSignal,
  onAccepted?: (runId: string) => void,
): Promise<RunResult | { type: 'error'; error: string }> {
  return streamEndpoint(
    `/api/harnesses/${id}/resume`,
    body,
    onEvent,
    signal,
    onAccepted,
  )
}

async function streamEndpoint(
  path: string,
  body: unknown,
  onEvent: (event: TraceEvent) => void,
  signal?: AbortSignal,
  onAccepted?: (runId: string) => void,
): Promise<RunResult | { type: 'error'; error: string }> {
  const response = await fetch(path, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  })
  if (!response.ok || !response.body) {
    let info: ApiError
    try {
      info = ((await response.json()) as { error: ApiError }).error
    } catch {
      info = { code: 'unreachable', message: `${response.status} ${response.statusText}` }
    }
    throw new HiveloomApiError(info)
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let outcome: RunResult | { type: 'error'; error: string } | null = null

  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split('\n')
    buffer = lines.pop() ?? ''
    for (const line of lines) {
      if (!line.trim()) continue
      const frame = JSON.parse(line)
      if (frame.type === 'run_accepted') {
        onAccepted?.(frame.run_id as string)
      } else if (frame.type === 'run_result' || frame.type === 'error') {
        outcome = frame
      } else {
        onEvent(frame as TraceEvent)
      }
    }
  }

  return outcome ?? { type: 'error', error: 'the run ended without a result' }
}
