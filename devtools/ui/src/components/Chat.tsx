import { useState } from 'react'
import { formatMs, preview, projectTrajectory } from '../trajectory'
import type { Span } from '../trajectory'
import type { RunWorkspace, Turn } from '../useRun'
import type { HarnessDetail, TraceEvent } from '../types'
import { taskGuideFor } from '../taskGuide'
import { Notice, StatusPill, VerdictChip } from './common'

/**
 * The conversation view of a workspace.
 *
 * This is where a harness UI differs from a chat UI: what matters is not only
 * the reply but *how it was reached*. Each assistant turn renders its work
 * inline — tool calls as cards that expand in place, verifiers as pass/fail
 * rows — while the full forensic record stays one tab away in Trajectory. Both
 * views project the same journal; neither is a summary of the other.
 *
 * Three things the thread says that a plain transcript would not: a message
 * delivered *into* a running turn is drawn as one, not folded in with the rest;
 * a version boundary is drawn where it fell, because the runs on either side
 * execute different harness versions; and a turn's files are named on the message
 * that carried them, because that is what the harness was actually told.
 */
export function Chat({
  harness,
  workspace,
  onTrust,
  onOpenTrajectory,
  onImprove,
  onNewRun,
}: {
  harness: HarnessDetail
  workspace: RunWorkspace
  onTrust: () => void
  onOpenTrajectory: (runId: string) => void
  onImprove: () => void
  onNewRun: () => void
}) {
  const { turns, busy } = workspace
  const guide = taskGuideFor(harness)

  return (
    <div className="thread">
      {workspace.trustNeeded && (
        <div style={{ maxWidth: 860, margin: '0 auto 16px' }}>
          <Notice
            icon="ph-shield-warning"
            tone="warn"
            title="Trust required"
            body="This harness has not been trusted, so it will not run."
            action={
              <button className="v-btn v-btn-ghost v-btn-sm" onClick={onTrust}>
                <i className="ph ph-shield-check" />
                Trust
              </button>
            }
          />
        </div>
      )}

      {turns.map((turn, index) => (
        <TurnView
          key={index}
          turn={turn}
          harnessName={harness.name}
          live={busy && index === turns.length - 1}
          onOpenTrajectory={onOpenTrajectory}
          onImprove={onImprove}
          onNewRun={onNewRun}
          inputHelp={guide.inputHelp}
        />
      ))}
    </div>
  )
}

function TurnView({
  turn,
  harnessName,
  live,
  onOpenTrajectory,
  onImprove,
  onNewRun,
  inputHelp,
}: {
  turn: Turn
  harnessName: string
  live: boolean
  onOpenTrajectory: (runId: string) => void
  onImprove: () => void
  onNewRun: () => void
  inputHelp: string
}) {
  if (turn.versionBoundary) {
    return (
      <div className="version-boundary">
        <span>
          harness evolved · {turn.versionBoundary.from.slice(0, 6)} →{' '}
          {turn.versionBoundary.to.slice(0, 6)} · new run
        </span>
      </div>
    )
  }

  if (turn.role === 'steer') {
    return (
      <div className="msg user steer rise">
        <div>
          <div className="steer-label">
            <i className="ph ph-arrow-bend-down-right" />
            steered mid-run
          </div>
          <div className="msg-bubble">{turn.content}</div>
        </div>
      </div>
    )
  }

  if (turn.role === 'user') {
    return (
      <div className="msg user rise">
        <div>
          <div className="msg-bubble">{turn.content}</div>
          {turn.attachments && turn.attachments.length > 0 && (
            <div className="msg-attachments mono">
              {turn.attachments.map((file) => (
                <span key={file.path} title={`sha256 ${file.sha256}`}>
                  <i className="ph ph-paperclip" />
                  {file.path}
                </span>
              ))}
            </div>
          )}
        </div>
      </div>
    )
  }

  const events = turn.events ?? []
  const trajectory = events.length > 0 ? projectTrajectory(events) : null
  const verificationFailed = Boolean(
    turn.result &&
      (turn.result.status === 'verify_failed' ||
        turn.result.verdicts.some((verdict) => !verdict.passed)),
  )

  return (
    <div className="msg assistant rise">
      <div className="v-label msg-who">{harnessName}</div>

      {trajectory && trajectory.spans.length > 0 && (
        <div className="work">
          {trajectory.spans.map((span) => (
            <WorkRow key={span.id} span={span} events={events} live={live} />
          ))}
        </div>
      )}

      {live && !turn.content && !turn.error && (
        <div className="work-row thinking">
          <i className="ph ph-circle-notch" style={{ animation: 'spin 1s linear infinite' }} />
          running…
        </div>
      )}

      {turn.error ? (
        <div className="msg-body err">{turn.error}</div>
      ) : turn.content ? (
        <MessageBody text={turn.content} />
      ) : null}

      {turn.result && (
        <>
          {turn.result.verdicts.length > 0 && (
            <div className="verdict-row">
              {turn.result.verdicts.map((verdict) => (
                <VerdictChip
                  key={verdict.verifier}
                  name={verdict.verifier}
                  passed={verdict.passed}
                  title={verdict.feedback}
                />
              ))}
            </div>
          )}
          <div className="msg-actions mono">
            <StatusPill status={turn.result.status} />
            <span>{turn.result.turns} turns</span>
            <span>${turn.result.cost_usd.toFixed(4)}</span>
            <span>{turn.result.duration_seconds.toFixed(1)}s</span>
            {turn.result.reason && <span>{turn.result.reason}</span>}
            <button
              className="msg-action"
              onClick={() => onOpenTrajectory(turn.result!.run_id)}
              title="Open this run in the Trajectory"
            >
              <i className="ph ph-list-magnifying-glass" />
              Inspect
            </button>
            <button
              className="msg-action"
              onClick={() => void navigator.clipboard?.writeText(turn.content)}
              title="Copy the reply"
            >
              <i className="ph ph-copy" />
            </button>
          </div>
          {!turn.result.ok && (
            <div className="run-next">
              <div className="run-next-copy">
                <div className="v-label">What next</div>
                <strong>
                  {verificationFailed
                    ? 'This run did not pass verification.'
                    : 'This run did not complete successfully.'}
                </strong>
                {verificationFailed ? (
                  <p>
                    First confirm the input matched the harness contract: {inputHelp} If it did,
                    inspect the evidence, then improve the harness from this recorded failure.
                  </p>
                ) : (
                  <p>
                    Inspect the trajectory to find where it stopped before deciding what to change.
                  </p>
                )}
              </div>
              <div className="run-next-actions">
                <button className="v-btn v-btn-ghost v-btn-sm" onClick={onNewRun}>
                  <i className="ph ph-arrow-counter-clockwise" />
                  Try another input
                </button>
                <button
                  className="v-btn v-btn-ghost v-btn-sm"
                  onClick={() => onOpenTrajectory(turn.result!.run_id)}
                >
                  <i className="ph ph-list-magnifying-glass" />
                  Inspect failure
                </button>
                <button className="v-btn v-btn-primary v-btn-sm" onClick={onImprove}>
                  <i className="ph ph-sparkle" />
                  Improve harness
                </button>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  )
}

/** One unit of work inside a turn: a model call, a tool call, or a verifier. */
function WorkRow({ span, events, live }: { span: Span; events: TraceEvent[]; live: boolean }) {
  const [open, setOpen] = useState(false)
  const call = events.find((event) => event.seq === span.startSeq)
  const result =
    span.endSeq !== null && span.endSeq !== span.startSeq
      ? events.find((event) => event.seq === span.endSeq)
      : null
  const elapsed = span.endMs === null ? (live ? '…' : 'open') : formatMs(span.durationMs)

  if (span.lane === 'model') {
    const text = String(result?.payload.text ?? '')
    return (
      <div className="work-row model">
        <i className="ph ph-brain" />
        <span className="work-name">Model</span>
        <span className="work-detail">{text ? preview(text, 120) : span.detail}</span>
        <span className="work-time mono">{elapsed}</span>
      </div>
    )
  }

  if (span.lane === 'verify') {
    return (
      <div className={`work-row verify${span.failed ? ' failed' : ''}`}>
        <i className={`ph ${span.failed ? 'ph-x-circle' : 'ph-check-circle'}`} />
        <span className="work-name">{span.label}</span>
        <span className="work-detail">{span.detail}</span>
      </div>
    )
  }

  return (
    <div className={`work-card${span.failed ? ' failed' : ''}`}>
      <button className="work-row tool" onClick={() => setOpen((value) => !value)}>
        <i className={`ph ${open ? 'ph-caret-down' : 'ph-caret-right'}`} />
        <span className="work-name">{span.label}</span>
        <span className="work-detail">{span.detail}</span>
        <span className="work-time mono">{elapsed}</span>
      </button>
      {open && (
        <div className="work-body">
          <div className="work-io">
            <span className="v-label">input</span>
            <pre>{JSON.stringify(call?.payload.input ?? {}, null, 2)}</pre>
          </div>
          <div className="work-io">
            <span className="v-label">{span.failed ? 'error' : 'result'}</span>
            <pre>
              {String(result?.payload.content ?? (live ? 'running…' : 'no result recorded'))}
            </pre>
          </div>
          <button
            className="msg-action"
            onClick={() =>
              void navigator.clipboard?.writeText(String(result?.payload.content ?? ''))
            }
          >
            <i className="ph ph-copy" />
            Copy
          </button>
        </div>
      )}
    </div>
  )
}

/* -------------------------------------------------------------- the reply */

type Block =
  | { kind: 'code'; lang: string; text: string }
  | { kind: 'para' | 'bullet' | 'h1' | 'h2'; text: string }

/**
 * The reply, rendered as the small subset of Markdown a harness actually
 * emits: fenced code, headings, bullets, and inline emphasis, bold and code.
 *
 * A subset rather than a library on purpose. Everything outside these forms
 * stays verbatim, so text a harness never meant as markup is not silently
 * restyled, and there is no HTML path at all — every leaf is a React text
 * node, which is what makes a reply from a model incapable of injecting
 * markup into this window.
 */
export function MessageBody({ text }: { text: string }) {
  return (
    <div className="msg-body">
      {parseBlocks(text).map((block, index) =>
        block.kind === 'code' ? (
          <div className="code-block" key={index}>
            <div className="code-block-head">
              <span className="mono">{block.lang || 'text'}</span>
              <button
                className="msg-action"
                onClick={() => void navigator.clipboard?.writeText(block.text)}
              >
                <i className="ph ph-copy" />
                Copy
              </button>
            </div>
            <pre>{block.text}</pre>
          </div>
        ) : block.kind === 'bullet' ? (
          <div className="md-bullet" key={index}>
            <span>•</span>
            <p>
              <Inline text={block.text} />
            </p>
          </div>
        ) : block.kind === 'para' ? (
          <p key={index}>
            <Inline text={block.text} />
          </p>
        ) : (
          <div className={`md-${block.kind}`} key={index}>
            <Inline text={block.text} />
          </div>
        ),
      )}
    </div>
  )
}

function parseBlocks(text: string): Block[] {
  const blocks: Block[] = []
  const chunks = text.split('```')
  chunks.forEach((chunk, index) => {
    // Odd chunks are inside a fence. An unterminated fence leaves a final odd
    // chunk, which is still code — the model was mid-block, not mid-prose.
    if (index % 2 === 1) {
      const newline = chunk.indexOf('\n')
      blocks.push({
        kind: 'code',
        lang: newline === -1 ? '' : chunk.slice(0, newline).trim(),
        text: (newline === -1 ? chunk : chunk.slice(newline + 1)).replace(/\n$/, ''),
      })
      return
    }
    for (const line of chunk.split('\n')) {
      const raw = line.trim()
      if (!raw) continue
      const heading = /^(#{1,6})\s+(.*)$/.exec(raw)
      if (heading) {
        blocks.push({ kind: heading[1].length === 1 ? 'h1' : 'h2', text: heading[2] })
        continue
      }
      const bullet = /^[-*]\s+(.*)$/.exec(raw)
      if (bullet) {
        blocks.push({ kind: 'bullet', text: bullet[1] })
        continue
      }
      blocks.push({ kind: 'para', text: raw })
    }
  })
  return blocks
}

const INLINE = /(\*\*[^*]+\*\*|__[^_]+__|`[^`]+`|\*[^*]+\*|_[^_]+_|\[[^\]]+\]\([^)]+\))/g

function Inline({ text }: { text: string }) {
  const parts: React.ReactNode[] = []
  let cursor = 0
  let match: RegExpExecArray | null
  INLINE.lastIndex = 0
  while ((match = INLINE.exec(text)) !== null) {
    if (match.index > cursor) parts.push(text.slice(cursor, match.index))
    const token = match[0]
    const key = `${match.index}`
    if (token.startsWith('**') || token.startsWith('__')) {
      parts.push(<strong key={key}>{token.slice(2, -2)}</strong>)
    } else if (token.startsWith('`')) {
      parts.push(
        <code className="md-code" key={key}>
          {token.slice(1, -1)}
        </code>,
      )
    } else if (token.startsWith('[')) {
      // The label only. A reply's URL is data, not something this window
      // should turn into a click target on the model's say-so.
      parts.push(
        <span className="md-link" key={key} title={token.slice(token.indexOf('(') + 1, -1)}>
          {token.slice(1, token.indexOf(']'))}
        </span>,
      )
    } else {
      parts.push(<em key={key}>{token.slice(1, -1)}</em>)
    }
    cursor = match.index + token.length
  }
  if (cursor < text.length) parts.push(text.slice(cursor))
  return <>{parts}</>
}
