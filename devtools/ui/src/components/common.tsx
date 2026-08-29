/**
 * Shared pieces, taken from hiveloom-cloud rather than invented here.
 *
 * `RUN_STATUS` and `when()` are ports of `web/app/(app)/harnesses/[slug]/runs-tab.tsx`;
 * the uppercase section label and the chip shapes are the same ones the cloud
 * uses on its harness pages. Keeping them identical is the point — the two
 * surfaces should read as one product.
 */

/** Run status → token. Ported verbatim from the cloud's runs tab. */
export const RUN_STATUS: Record<string, string> = {
  success: 'var(--ok)',
  verify_failed: 'var(--warn)',
  guardrail_halt: 'var(--warn)',
  max_turns: 'var(--warn)',
  error: 'var(--err)',
  incomplete: 'var(--mut)',
}

export function statusColor(status: string): string {
  return RUN_STATUS[status] ?? 'var(--mut)'
}

export function when(iso: string | null | undefined): string {
  if (!iso) return '—'
  const date = new Date(iso)
  return Number.isNaN(date.getTime())
    ? iso
    : date.toLocaleString(undefined, {
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
      })
}

export function Label({ children }: { children: React.ReactNode }) {
  return <div className="v-label">{children}</div>
}

/** A status word in its own colour, on the tinted pill the cloud uses. */
export function StatusPill({ status }: { status: string }) {
  const color = statusColor(status)
  return (
    <span
      className="mono"
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 6,
        fontSize: 11.5,
        color,
        background: 'var(--tint)',
        border: '1px solid var(--border)',
        borderRadius: 6,
        padding: '3px 9px',
      }}
    >
      <span
        style={{ width: 6, height: 6, borderRadius: 999, background: color, flex: 'none' }}
      />
      {status}
    </span>
  )
}

/** The cloud's ops chip, reused for validator verdicts. */
export function VerdictChip({
  name,
  passed,
  title,
}: {
  name: string
  passed: boolean
  title?: string
}) {
  // Mixed from the token rather than written as a literal: the same chip has to
  // read on a light ground, where the dark palette's green is the wrong green.
  const color = passed ? 'var(--ok)' : 'var(--err)'
  const bg = `color-mix(in srgb, ${color} 12%, transparent)`
  const border = `color-mix(in srgb, ${color} 25%, transparent)`
  return (
    <span
      className="mono"
      title={title}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 5,
        fontSize: 11.5,
        color,
        background: bg,
        border: `1px solid ${border}`,
        borderRadius: 6,
        padding: '3px 9px',
      }}
    >
      <i className={passed ? 'ph ph-check' : 'ph ph-x'} />
      {name}
    </span>
  )
}

export function Stat({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div>
      <div style={{ fontSize: 19, fontWeight: 600, letterSpacing: '-0.02em', color }}>{value}</div>
      <div className="v-label" style={{ marginTop: 3 }}>
        {label}
      </div>
    </div>
  )
}

export function StatRow({ children }: { children: React.ReactNode }) {
  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(96px,1fr))', gap: 18 }}>
      {children}
    </div>
  )
}

/** An inline message: neutral, or carrying an error/warning token. */
export function Notice({
  icon,
  tone = 'dim',
  title,
  body,
  action,
}: {
  icon: string
  tone?: 'dim' | 'warn' | 'err' | 'ok'
  title: string
  body?: string
  action?: React.ReactNode
}) {
  const color = { dim: 'var(--dim)', warn: 'var(--warn)', err: 'var(--err)', ok: 'var(--ok)' }[tone]
  return (
    <div className="notice rise" style={{ borderColor: tone === 'dim' ? undefined : color }}>
      <i className={`ph ${icon}`} style={{ color }} />
      <div style={{ minWidth: 0, flex: 1 }}>
        <div style={{ color: 'var(--text)', fontWeight: 500 }}>{title}</div>
        {body && (
          <div style={{ color: 'var(--dim)', fontSize: 13, marginTop: 2 }}>{body}</div>
        )}
      </div>
      {action}
    </div>
  )
}
