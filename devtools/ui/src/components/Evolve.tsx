/**
 * The evolve loop: propose from real failures, read the gate, apply or reject,
 * then judge the result against the version it replaced.
 *
 * The gate is the point of this screen, so it is shown rather than summarised:
 * what the model asked for, what the gate accepted, what it refused and why —
 * `guardrails`, `model`, `logging.redact`, `extensions`, `hooks`,
 * `mcp_servers` and `evolution.auto_propose` can never be evolved, and a
 * refusal here is that rule working, not an error.
 *
 * Code and prompt-prose changes are approved per file. A file left unchecked
 * stays pending: silence is a refusal, not consent.
 *
 * Judging the result is the version graph's job, not this screen's: whether an
 * evolution helped is a question about two versions' evidence, and that is
 * where both versions are.
 */
import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import type { ApplyResult, HarnessDetail, Proposal } from '../types'
import { Label, Notice, when } from './common'

export function Evolve({
  harness,
  evolveModel,
  onApplied,
  onCompare,
}: {
  harness: HarnessDetail
  /** `provider/model-id` to draft with, or '' for hiveloom's strong-model default. */
  evolveModel: string
  onApplied: () => Promise<void>
  onCompare: () => void
}) {
  const [proposals, setProposals] = useState<Proposal[] | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [approvedCode, setApprovedCode] = useState<string[]>([])
  const [approvedProse, setApprovedProse] = useState<string[]>([])
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [note, setNote] = useState<string | null>(null)
  const [applied, setApplied] = useState<ApplyResult | null>(null)

  const load = useCallback(async () => {
    try {
      const rows = await api.proposals(harness.id)
      setProposals(rows)
      setSelected((current) => current ?? rows.find((row) => row.status === 'pending')?.id ?? null)
    } catch (exc) {
      setError(String(exc))
      setProposals([])
    }
  }, [harness.id])

  useEffect(() => {
    setProposals(null)
    setSelected(null)
    setApplied(null)
    setNote(null)
    void load()
  }, [load])

  const open = proposals?.find((row) => row.id === selected) ?? null

  const propose = async (fromParent = false) => {
    setBusy(fromParent ? 'propose-parent' : 'propose')
    setError(null)
    setNote(null)
    try {
      const result = await api.propose(harness.id, evolveModel || undefined, fromParent)
      if (!result.changed) {
        setNote(
          result.reason ??
            'Nothing to evolve: no failures recorded for this harness version. Run it again to collect fresh ones.',
        )
      } else {
        setSelected(result.id ?? null)
      }
      await load()
    } catch (exc) {
      setError(String(exc))
    } finally {
      setBusy(null)
    }
  }

  const apply = async () => {
    if (!open) return
    setBusy('apply')
    setError(null)
    try {
      const result = await api.applyProposal(
        harness.id,
        open.id,
        approvedCode,
        approvedProse,
      )
      setApplied(result)
      await Promise.all([load(), onApplied()])
    } catch (exc) {
      setError(String(exc))
    } finally {
      setBusy(null)
    }
  }

  const reject = async () => {
    if (!open) return
    setBusy('reject')
    setError(null)
    try {
      await api.rejectProposal(open.id, 'rejected in the workbench')
      await load()
    } catch (exc) {
      setError(String(exc))
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="pane">
      <div className="evolve-head">
        <div>
          <Label>Evolution</Label>
          <p className="evolve-lede">
            A proposal is drafted from this version's recorded failures by a strong model, gated,
            and queued. Applying is always a separate, human call.
          </p>
          {harness.fork && (
            <p className="evolve-note">
              This folder is a fork of {harness.fork.parent_run_id} at turn {harness.fork.at_turn}.
              Until you resume it, it has no runs of its own — so the failures worth learning from
              are the parent's, and that is the second button.
            </p>
          )}
        </div>
        <div className="evolve-actions">
          <button
            className="v-btn v-btn-primary"
            onClick={() => void propose()}
            disabled={busy !== null}
          >
            {busy === 'propose' ? (
              <i className="ph ph-circle-notch" style={{ animation: 'spin 1s linear infinite' }} />
            ) : (
              <i className="ph ph-sparkle" />
            )}
            Propose from failures
          </button>
          {/* Offered only on a fork, and never instead of the button above:
              which version's evidence to use is the reader's call, and a
              silent fallback would hide which one a proposal came from. */}
          {harness.fork && (
            <button
              className="v-btn"
              onClick={() => void propose(true)}
              disabled={busy !== null}
              title="Analyse the parent run's version instead of this fork's"
            >
              {busy === 'propose-parent' ? (
                <i className="ph ph-circle-notch" style={{ animation: 'spin 1s linear infinite' }} />
              ) : (
                <i className="ph ph-git-fork" />
              )}
              Propose from parent's failures
            </button>
          )}
        </div>
      </div>

      {note && (
        <div style={{ marginBottom: 16 }}>
          <Notice icon="ph-info" tone="warn" title="Nothing to evolve" body={note} />
        </div>
      )}
      {error && (
        <div style={{ marginBottom: 16 }}>
          <Notice icon="ph-warning-octagon" tone="err" title="Evolution failed" body={error} />
        </div>
      )}
      {applied && (
        <div style={{ marginBottom: 16 }}>
          <Notice
            icon="ph-check-circle"
            tone="ok"
            title={`Applied — evolved #${applied.counter}`}
            body={`${applied.old_version_hash.slice(0, 12)} → ${applied.new_version_hash.slice(0, 12)}. ${
              applied.applied_code.length
            } code file(s) written${
              applied.pending_code?.length ? `, ${applied.pending_code.length} left pending` : ''
            }; ${applied.applied_prose?.length ?? 0} prompt file(s) written${
              applied.pending_prose?.length
                ? `, ${applied.pending_prose.length} left pending`
                : ''
            }. Run the harness again to give the new version evidence of its own.`}
            action={
              <button className="v-btn v-btn-ghost v-btn-sm" onClick={onCompare}>
                <i className="ph ph-git-diff" />
                Compare
              </button>
            }
          />
        </div>
      )}

      {proposals === null ? (
        <div className="empty">Loading…</div>
      ) : proposals.length === 0 ? (
        <div className="empty">
          <i className="ph ph-flask" style={{ fontSize: 26, display: 'block', marginBottom: 10 }} />
          No proposals yet. Failures are the input — run the harness, then propose.
        </div>
      ) : (
        <div className="evolve-body">
          <div className="proposal-list">
            {proposals.map((row) => (
              <button
                key={row.id}
                className="proposal-row"
                data-on={selected === row.id ? '1' : '0'}
                onClick={() => {
                  setSelected(row.id)
                  setApprovedCode([])
                  setApprovedProse([])
                  setApplied(null)
                }}
              >
                <span className={`proposal-status ${row.status}`}>{row.status}</span>
                <span className="proposal-title">{row.rationale || row.proposal.rationale}</span>
                <span className="mono proposal-meta">
                  {row.spec_version_hash.slice(0, 8)} · {row.trigger} · {when(row.created_at)}
                </span>
              </button>
            ))}
          </div>

          {open && (
            <ProposalDetail
              proposal={open}
              approvedCode={approvedCode}
              approvedProse={approvedProse}
              busy={busy}
              onToggleCode={(file) =>
                setApprovedCode((current) =>
                  current.includes(file)
                    ? current.filter((item) => item !== file)
                    : [...current, file],
                )
              }
              onToggleProse={(file) =>
                setApprovedProse((current) =>
                  current.includes(file)
                    ? current.filter((item) => item !== file)
                    : [...current, file],
                )
              }
              onApply={() => void apply()}
              onReject={() => void reject()}
            />
          )}
        </div>
      )}
    </div>
  )
}

function ProposalDetail({
  proposal,
  approvedCode,
  approvedProse,
  busy,
  onToggleCode,
  onToggleProse,
  onApply,
  onReject,
}: {
  proposal: Proposal
  approvedCode: string[]
  approvedProse: string[]
  busy: string | null
  onToggleCode: (file: string) => void
  onToggleProse: (file: string) => void
  onApply: () => void
  onReject: () => void
}) {
  const gate = proposal.gate
  const pending = proposal.status === 'pending'
  return (
    <div className="proposal-detail">
      <div className="inspector-callout">{proposal.proposal.rationale || proposal.rationale}</div>

      <section>
        <Label>Accepted by the gate · {gate.accepted.length}</Label>
        {gate.accepted.length === 0 ? (
          <p className="evolve-note">Nothing in this proposal cleared the gate.</p>
        ) : (
          gate.accepted.map((change) => (
            <div className="change-row" key={change.path}>
              <code>{change.path}</code>
              <pre>{JSON.stringify(change.value, null, 2)}</pre>
              {change.rationale && <span className="evolve-note">{change.rationale}</span>}
            </div>
          ))
        )}
      </section>

      {gate.rejected.length > 0 && (
        <section>
          <Label>Refused · {gate.rejected.length}</Label>
          {gate.rejected.map((item) => (
            <div className="change-row refused" key={item.path}>
              <code>{item.path}</code>
              <span className="evolve-note">{item.reason}</span>
            </div>
          ))}
        </section>
      )}

      {gate.code_changes.length > 0 && (
        <section>
          <Label>Code · {gate.code_changes.length}</Label>
          <p className="evolve-note">
            Each file is approved on its own. Anything left unchecked stays pending.
          </p>
          {gate.code_changes.map((change) => (
            <div className="change-row code" key={change.file}>
              <label className="code-approve">
                <input
                  type="checkbox"
                  checked={approvedCode.includes(change.file)}
                  disabled={!pending}
                  onChange={() => onToggleCode(change.file)}
                />
                <code>{change.file}</code>
              </label>
              {change.rationale && <span className="evolve-note">{change.rationale}</span>}
              <pre className="code-source">{change.source}</pre>
            </div>
          ))}
        </section>
      )}

      {(gate.prose_changes ?? []).length > 0 && (
        <section>
          <Label>Prompt prose · {(gate.prose_changes ?? []).length}</Label>
          <p className="evolve-note">
            These files are declared playbook prompts. They use a separate approval from code.
          </p>
          {(gate.prose_changes ?? []).map((change) => (
            <div className="change-row code" key={change.file}>
              <label className="code-approve">
                <input
                  type="checkbox"
                  checked={approvedProse.includes(change.file)}
                  disabled={!pending}
                  onChange={() => onToggleProse(change.file)}
                />
                <code>{change.file}</code>
              </label>
              {change.rationale && <span className="evolve-note">{change.rationale}</span>}
              <pre className="code-source">{change.source}</pre>
            </div>
          ))}
        </section>
      )}

      {pending ? (
        <div className="proposal-actions">
          <button className="v-btn v-btn-ghost" onClick={onReject} disabled={busy !== null}>
            <i className="ph ph-x" />
            Reject
          </button>
          <button className="v-btn v-btn-primary" onClick={onApply} disabled={busy !== null}>
            {busy === 'apply' ? (
              <i className="ph ph-circle-notch" style={{ animation: 'spin 1s linear infinite' }} />
            ) : (
              <i className="ph ph-check" />
            )}
            Apply{' '}
            {approvedCode.length + approvedProse.length > 0
              ? `with ${approvedCode.length} code and ${approvedProse.length} prompt file(s)`
              : 'YAML only'}
          </button>
        </div>
      ) : (
        <p className="evolve-note">
          This proposal is {proposal.status}
          {proposal.resolved_at ? ` · ${when(proposal.resolved_at)}` : ''}.
        </p>
      )}
    </div>
  )
}
