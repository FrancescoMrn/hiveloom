import type { Delegation, Referral, RunResult } from '../types'
import { StatusPill } from './common'

/**
 * The two message renderers the copilot conversation is built from: what a
 * turn handed to a peer, and the reply itself.
 *
 * They live apart from any one view because both the chat thread and the
 * artifact canvas render the same reply text and the same delegation record —
 * projected from a result, never from a live trace, so a restored conversation
 * shows exactly what a fresh one does.
 */

/**
 * What this turn handed to a peer, and who it would have handed it to.
 *
 * Both halves of the same fact: `delegations` is work that actually happened
 * somewhere else and is charged to this run, `referrals` is the harness that
 * would have fitted but was not used — which is the answer to "I am not the
 * right harness for this" rather than a silent guess.
 *
 * Rendered from the result rather than from the trace, so a restored
 * conversation shows it too; a result recorded before delegation existed
 * carries neither list and renders nothing.
 */
export function DelegationTrail({
  result,
  onOpenRun,
}: {
  result: Pick<RunResult, 'delegations' | 'referrals'>
  /** Navigate to a run id — the same route Inspect and the fork links take. */
  onOpenRun: (runId: string) => void
}) {
  const delegations: Delegation[] = result.delegations ?? []
  const referrals: Referral[] = result.referrals ?? []
  if (delegations.length === 0 && referrals.length === 0) return null
  return (
    <div className="delegation-trail">
      {delegations.map((record, index) => (
        <div className="delegation-row" key={`${record.harness}-${record.run_id || index}`}>
          <i className="ph ph-arrow-bend-up-right" />
          <span className="delegation-name">delegated to {record.harness}</span>
          <StatusPill status={record.status} />
          <span className="mono">${(record.cost_usd ?? 0).toFixed(4)}</span>
          <span className="mono">{record.turns ?? 0} turns</span>
          {record.reason && <span className="delegation-reason">{record.reason}</span>}
          {record.run_id && (
            <button
              className="msg-action"
              onClick={() => onOpenRun(record.run_id)}
              title={`Open ${record.run_id}`}
            >
              <i className="ph ph-list-magnifying-glass" />
              Open run
            </button>
          )}
        </div>
      ))}
      {referrals.map((referral, index) => (
        <div className="delegation-row referral" key={`${referral.harness}-${index}`}>
          <i className="ph ph-signpost" />
          <span className="delegation-name">{referral.harness}</span>
          <span className="delegation-reason" title={referral.description}>
            success {Math.round((referral.success_rate ?? 0) * 100)}% over{' '}
            {referral.total_runs ?? 0} runs · {referral.reason}
          </span>
        </div>
      ))}
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
