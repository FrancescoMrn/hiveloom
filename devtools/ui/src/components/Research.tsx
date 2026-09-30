/**
 * Research programs: a director model improves this harness through
 * experiments the engine measures.
 *
 * The screen is the program's record, not a chat: the charter the user wrote,
 * the budget by pool, every hypothesis with its verdict, what was kept, why
 * the program stopped, and the proposal it queued. The director's reasoning is
 * in its own journals; what is shown here is what the engine decided. Nothing
 * reaches the live harness from this screen — the queued proposal is reviewed
 * and applied in Improve, like any other.
 */
import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import type {
  HarnessDetail,
  ResearchContract,
  ResearchDetail,
  ResearchExperiment,
  ResearchProgramRow,
  ResearchQuestion,
} from '../types'
import { workbenchModels } from '../models'
import { Label, Notice, Stat, StatRow, when } from './common'
import { useProviders } from './Settings'

const VERDICT_TONE: Record<string, string> = {
  confirmed: 'var(--ok)',
  improved: 'var(--ok)',
  regressed: 'var(--err)',
  refuted: 'var(--warn)',
  futile: 'var(--warn)',
  shifted: 'var(--err)',
  inconclusive: 'var(--dim)',
  unaffordable: 'var(--dim)',
}

export function Research({
  harness,
  onOpenImprove,
  onOpenRun,
}: {
  harness: HarnessDetail
  onOpenImprove: () => void
  /** Open one run's journal in the Trace view. */
  onOpenRun?: (runId: string) => void
}) {
  const [programs, setPrograms] = useState<ResearchProgramRow[] | null>(null)
  const [templates, setTemplates] = useState<Record<string, string>>({})
  const [form, setForm] = useState<CharterForm | null>(null)
  const models = workbenchModels(useProviders(harness.id))
  const [selected, setSelected] = useState<string | null>(null)
  const [detail, setDetail] = useState<ResearchDetail | null>(null)
  const [creating, setCreating] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const loadPrograms = useCallback(async () => {
    try {
      const listed = await api.researchPrograms(harness.id)
      setPrograms(listed.programs)
      setTemplates(listed.charter_templates ?? { 'research.yaml': listed.charter_template })
      setForm(listed.form ?? null)
      setSelected((current) => current ?? listed.programs.at(-1)?.name ?? null)
    } catch (exc) {
      setError(String(exc))
      setPrograms([])
    }
  }, [harness.id])

  const loadDetail = useCallback(async () => {
    if (!selected) {
      setDetail(null)
      return
    }
    try {
      setDetail(await api.research(harness.id, selected))
    } catch (exc) {
      setError(String(exc))
    }
  }, [harness.id, selected])

  useEffect(() => {
    setPrograms(null)
    setSelected(null)
    setDetail(null)
    setCreating(false)
    void loadPrograms()
  }, [loadPrograms])

  useEffect(() => {
    void loadDetail()
  }, [loadDetail])

  // A running program is polled; an idle one is read once.
  const running = Boolean(detail?.job?.running)
  useEffect(() => {
    if (!running) return
    const timer = window.setInterval(() => {
      void loadDetail()
    }, 1500)
    return () => window.clearInterval(timer)
  }, [loadDetail, running])
  useEffect(() => {
    if (!running) void loadPrograms()
  }, [loadPrograms, running])

  const act = async (action: () => Promise<unknown>) => {
    setError(null)
    try {
      await action()
      await loadDetail()
    } catch (exc) {
      setError(String(exc))
    }
  }

  return (
    <div className="pane research-pane">
      <div className="evolve-head">
        <div>
          <Label>Research · evolve autonomously</Label>
          <p className="evolve-lede">
            Evolve, without you at every step. A director model studies this harness's failures,
            registers hypotheses and designs changes round after round; the engine runs every
            experiment on a copy, keeps only what it measures as better, reads a sealed split
            once, and queues one proposal for you to review in Improve.
          </p>
        </div>
        <div className="evolve-actions">
          <button className="v-btn" onClick={() => setCreating((value) => !value)}>
            <i className={`ph ${creating ? 'ph-x' : 'ph-flask'}`} />
            {creating ? 'Cancel' : 'New program'}
          </button>
        </div>
      </div>

      {error && (
        <div style={{ marginBottom: 16 }}>
          <Notice
            icon="ph-warning-octagon"
            tone="err"
            title="Research problem"
            body={error}
            action={<button className="icon-btn" onClick={() => setError(null)}><i className="ph ph-x" /></button>}
          />
        </div>
      )}

      {creating && (
        <NewProgram
          form={form}
          models={models}
          templates={templates}
          onCreate={async (name, charter) => {
            setError(null)
            try {
              const created = await api.createResearch(harness.id, name, charter)
              setCreating(false)
              setSelected(created.name)
              setDetail(created)
              await loadPrograms()
            } catch (exc) {
              setError(String(exc))
            }
          }}
        />
      )}

      {programs === null ? (
        <div className="empty">Loading…</div>
      ) : programs.length === 0 && !creating ? (
        <div className="empty">
          <i className="ph ph-flask" style={{ fontSize: 26, display: 'block', marginBottom: 10 }} />
          No research programs yet. A program needs an eval in this folder and a charter that says
          what it may change and spend — start one with “New program”.
        </div>
      ) : (
        <div className="research-body">
          {programs.length > 1 && (
            <div className="research-tabs">
              {programs.map((row) => (
                <button
                  key={row.name}
                  className="research-tab"
                  data-on={selected === row.name ? '1' : '0'}
                  onClick={() => setSelected(row.name)}
                >
                  {row.running && <span className="dot live" />}
                  {row.name}
                  <small>{row.status}</small>
                </button>
              ))}
            </div>
          )}
          {detail && (
            <ProgramView
              detail={detail}
              onRun={(until) => void act(() => api.runResearch(harness.id, detail.name, until))}
              onApprove={(contract) => void act(() => api.approveResearch(harness.id, detail.name, contract))}
              onAnswer={(id, answer) => void act(() => api.answerResearch(harness.id, detail.name, id, answer))}
              onStop={() => void act(() => api.stopResearch(harness.id, detail.name))}
              onOpenImprove={onOpenImprove}
              onOpenRun={onOpenRun}
            />
          )}
        </div>
      )}
    </div>
  )
}

export interface CharterForm {
  goal: string
  evals: string[]
  levers: { path: string; default: boolean }[]
  tools: { name: string; kind: string; tags: string[]; default: string | null }[]
  director: string
  defaults: { budget: number; rounds: number; holdout: number }
}

function NewProgram({
  form,
  models,
  templates,
  onCreate,
}: {
  form: CharterForm | null
  models: string[]
  templates: Record<string, string>
  onCreate: (name: string, charter: string) => Promise<void>
}) {
  const files = Object.keys(templates)
  const [mode, setMode] = useState<'form' | 'yaml'>(form ? 'form' : 'yaml')
  const [file, setFile] = useState(files[0] ?? '')
  const [name, setName] = useState('')
  const [charter, setCharter] = useState(templates[files[0]] ?? '')
  const [busy, setBusy] = useState(false)
  // The form's fields, seeded from what the harness allows.
  const [goal, setGoal] = useState(form?.goal ?? '')
  const [evalFile, setEvalFile] = useState(form?.evals[0] ?? 'eval.yaml')
  const [levers, setLevers] = useState<string[]>(
    form?.levers.filter((lever) => lever.default).map((lever) => lever.path) ?? [])
  const [budget, setBudget] = useState(form?.defaults.budget ?? 1)
  const [rounds, setRounds] = useState(form?.defaults.rounds ?? 4)
  const [holdout, setHoldout] = useState(form?.defaults.holdout ?? 0.25)
  const [director, setDirector] = useState(form?.director ?? '')
  const [toolModes, setToolModes] = useState<Record<string, string>>(
    Object.fromEntries((form?.tools ?? []).map((tool) => [tool.name, tool.default ?? ''])))
  useEffect(() => {
    const first = Object.keys(templates)[0] ?? ''
    setFile(first)
    setCharter(templates[first] ?? '')
  }, [templates])
  useEffect(() => {
    if (!form) return
    setGoal(form.goal)
    setEvalFile(form.evals[0] ?? 'eval.yaml')
    setLevers(form.levers.filter((lever) => lever.default).map((lever) => lever.path))
    setDirector(form.director)
    setToolModes(Object.fromEntries(form.tools.map((tool) => [tool.name, tool.default ?? ''])))
  }, [form])

  const unclassified = (form?.tools ?? []).filter((tool) => !toolModes[tool.name])
  const valid = /^[a-z0-9][a-z0-9-]{0,47}$/.test(name) && (
    mode === 'yaml' || (goal.trim() !== '' && levers.length > 0 && director.includes('/')
      && unclassified.length === 0 && budget > 0))

  const fromForm = () => JSON.stringify({
    goal: goal.trim(),
    eval: evalFile,
    holdout,
    levers,
    budget: { usd: budget, rounds },
    stop: { no_progress_rounds: 2 },
    execution: { tools: Object.fromEntries(
      Object.entries(toolModes).filter(([tool, value]) =>
        value && (form?.tools.find((t) => t.name === tool)?.default ?? null) !== value)) },
    models: { director },
  }, null, 2)

  return (
    <section className="research-new">
      <div className="research-new-head">
        <Label>New program</Label>
        <div className="research-tabs">
          {form && (
            <button className="research-tab" data-on={mode === 'form' ? '1' : '0'} onClick={() => setMode('form')}>
              Form
            </button>
          )}
          <button
            className="research-tab"
            data-on={mode === 'yaml' ? '1' : '0'}
            onClick={() => {
              if (mode === 'form' && form) setCharter(fromForm())
              setMode('yaml')
            }}
          >
            Edit YAML
          </button>
        </div>
      </div>
      <p className="evolve-note">
        The charter is yours: the engine validates it and never changes it. Starting a program
        runs nothing and spends nothing until you press Run.
      </p>
      <label className="research-field">
        <span>Name</span>
        <input className="research-name mono" placeholder="a-z, 0-9, dashes" value={name}
          onChange={(event) => setName(event.target.value.trim())} />
      </label>
      {mode === 'form' && form ? (
        <>
          <label className="research-field">
            <span>Goal</span>
            <textarea className="research-charter" rows={2} value={goal}
              onChange={(event) => setGoal(event.target.value)} />
          </label>
          <div className="research-field-row">
            <label className="research-field">
              <span>Eval</span>
              {form.evals.length ? (
                <select value={evalFile} onChange={(event) => setEvalFile(event.target.value)}>
                  {form.evals.map((item) => <option key={item}>{item}</option>)}
                </select>
              ) : (
                <em className="evolve-note">No eval runs this harness — use Edit YAML for concepts mode.</em>
              )}
            </label>
            <label className="research-field">
              <span>Budget (USD)</span>
              <input type="number" min={0.01} step={0.1} value={budget}
                onChange={(event) => setBudget(Number(event.target.value))} />
            </label>
            <label className="research-field">
              <span>Rounds</span>
              <input type="number" min={1} max={50} value={rounds}
                onChange={(event) => setRounds(Number(event.target.value))} />
            </label>
            <label className="research-field">
              <span>Sealed share</span>
              <input type="number" min={0} max={0.8} step={0.05} value={holdout}
                onChange={(event) => setHoldout(Number(event.target.value))} />
            </label>
          </div>
          <div className="research-field">
            <span>Levers — what the director may change</span>
            <div className="research-checks">
              {form.levers.map((lever) => (
                <label key={lever.path} className="mono">
                  <input
                    type="checkbox"
                    checked={levers.includes(lever.path)}
                    onChange={(event) => setLevers((current) => event.target.checked
                      ? [...current, lever.path]
                      : current.filter((item) => item !== lever.path))}
                  />
                  {lever.path}
                </label>
              ))}
            </div>
          </div>
          <label className="research-field">
            <span>Director model</span>
            <input className="mono" list="research-director-models" value={director}
              onChange={(event) => setDirector(event.target.value.trim())} />
            <datalist id="research-director-models">
              {[...new Set([form.director, ...models])].map((model) => <option key={model} value={model} />)}
            </datalist>
          </label>
          {form.tools.length > 0 && (
            <div className="research-field">
              <span>Tools — how research runs may use them</span>
              {form.tools.map((tool) => (
                <label key={tool.name} className="research-tool mono">
                  {tool.name} <small>{tool.kind}{tool.tags.length ? ` · ${tool.tags.join(', ')}` : ''}</small>
                  <select value={toolModes[tool.name] ?? ''}
                    onChange={(event) => setToolModes((current) => ({ ...current, [tool.name]: event.target.value }))}>
                    {!tool.default && <option value="">choose…</option>}
                    <option value="allow">allow — runs as declared</option>
                    <option value="replay">replay — recorded results only</option>
                    <option value="deny">deny — removed</option>
                    {tool.default === 'sandbox' && <option value="sandbox">sandbox — effects stay in the copy</option>}
                  </select>
                </label>
              ))}
              {unclassified.length > 0 && (
                <em className="evolve-note">
                  {unclassified.map((tool) => tool.name).join(', ')} has effects the engine cannot bound: choose how it may run.
                </em>
              )}
            </div>
          )}
        </>
      ) : (
        <>
          {files.length > 1 && (
            <div className="research-tabs">
              {files.map((item) => (
                <button
                  key={item}
                  className="research-tab"
                  data-on={item === file ? '1' : '0'}
                  onClick={() => {
                    setFile(item)
                    setCharter(templates[item])
                  }}
                >
                  {item}
                </button>
              ))}
            </div>
          )}
          <textarea
            className="research-charter mono"
            spellCheck={false}
            value={charter}
            onChange={(event) => setCharter(event.target.value)}
            rows={Math.min(28, Math.max(12, charter.split('\n').length + 1))}
          />
        </>
      )}
      <div className="proposal-actions">
        <button
          className="v-btn v-btn-primary"
          disabled={!valid || busy}
          onClick={async () => {
            setBusy(true)
            try {
              await onCreate(name, mode === 'form' && form ? fromForm() : charter)
            } finally {
              setBusy(false)
            }
          }}
        >
          <i className={`ph ${busy ? 'ph-circle-notch spin' : 'ph-check'}`} />
          Start program
        </button>
      </div>
    </section>
  )
}

function ProgramView({
  detail,
  onRun,
  onStop,
  onApprove,
  onAnswer,
  onOpenImprove,
  onOpenRun,
}: {
  detail: ResearchDetail
  onRun: (until: 'unit' | 'round' | 'done') => void
  onStop: () => void
  onApprove: (contract?: string) => void
  onAnswer: (questionId: string, answer: string) => void
  onOpenImprove: () => void
  onOpenRun?: (runId: string) => void
}) {
  const openQuestions = (detail.question_list ?? []).filter((q) => q.status === 'open')
  const running = Boolean(detail.job?.running)
  const finished = detail.unit === 'done'
  const claims = Object.fromEntries(detail.hypotheses.map((h) => [h.id, h]))
  const lastStep = detail.job?.steps.at(-1)
  return (
    <div className="research-program">
      <div className="research-status">
        <div>
          <h3>{detail.name}</h3>
          <p className="evolve-note">{detail.goal}</p>
          <span className="mono research-meta">
            {detail.status} · unit {detail.unit} · round {detail.round}/
            {String((detail.charter.budget as { rounds?: number } | undefined)?.rounds ?? '?')} · incumbent{' '}
            {detail.incumbent} · {detail.split.working} working / {detail.split.holdout} sealed cases ·
            director {detail.charter.models.director}
          </span>
        </div>
        <div className="evolve-actions">
          {running ? (
            <>
              <span className="live-chip"><span className="dot" /> {lastStep ? `${lastStep.unit}: ${lastStep.outcome ?? ''}` : 'starting'}</span>
              <button className="v-btn" onClick={onStop} title="Finish the current unit, then confirm and report">
                <i className="ph ph-stop" /> Stop
              </button>
            </>
          ) : finished || detail.awaiting ? null : (
            <>
              <button className="v-btn" onClick={() => onRun('unit')} title="Advance exactly one unit">
                <i className="ph ph-step-forward" /> Step
              </button>
              <button className="v-btn" onClick={() => onRun('round')} title="Run until this round is interpreted">
                <i className="ph ph-arrow-clockwise" /> Run a round
              </button>
              <button className="v-btn v-btn-primary" onClick={() => onRun('done')} title="Run until a stop condition, confirm and report">
                <i className="ph ph-play" /> Run to the end
              </button>
              <button className="v-btn v-btn-ghost" onClick={onStop} title="Confirm what was kept and report at the next run">
                <i className="ph ph-stop" /> Stop
              </button>
            </>
          )}
        </div>
      </div>

      {detail.progress && detail.progress.total > 0 && (
        <div className="research-progress" title={`${detail.progress.purpose}: ${detail.progress.completed} of ${detail.progress.total} cells`}>
          <span className="mono research-measure">
            {detail.progress.purpose?.replace(':', ' ')} · {detail.progress.completed}/{detail.progress.total} cells
          </span>
          <div className="research-progress-bar">
            <div style={{ width: `${Math.round((detail.progress.completed / detail.progress.total) * 100)}%` }} />
          </div>
        </div>
      )}

      {detail.blocked_reason && (
        <Notice icon="ph-prohibit" tone="err" title="The program is blocked" body={detail.blocked_reason} />
      )}

      {detail.draft_contract && (
        <ContractApproval
          contract={detail.draft_contract}
          samples={detail.sample_cases ?? []}
          onApprove={onApprove}
        />
      )}

      {openQuestions.length > 0 && (
        <section>
          <Label>Questions for you · {openQuestions.length} open</Label>
          <p className="evolve-note">
            Your labels decide whether a judge may score a criterion. The program keeps working
            while they wait.
          </p>
          {openQuestions.map((question) => (
            <QuestionCard key={question.id} question={question} onAnswer={onAnswer} />
          ))}
        </section>
      )}

      {detail.contract && (
        <section>
          <Label>Evaluation contract · v{detail.contract_version}</Label>
          {detail.contract.criteria.map((criterion) => {
            const trust = detail.trust?.find((row) => row.criterion === criterion.id)
            return (
              <div className="change-row" key={criterion.id}>
                <code>{criterion.id}</code>{' '}
                <span className="mono research-measure" style={{ color: trust?.measured ? 'var(--ok)' : 'var(--warn)' }}>
                  {trust?.measured ? 'measured' : 'unmeasured'} · {trust?.how ?? criterion.check.kind}
                </span>
                <div className="evolve-note">{criterion.says}</div>
              </div>
            )
          })}
        </section>
      )}

      {detail.job?.error && (
        <Notice icon="ph-warning-octagon" tone="err" title="The last run of this program failed" body={detail.job.error} />
      )}

      <section>
        <Label>Budget</Label>
        <StatRow>
          {Object.entries(detail.budget).map(([pool, values]) => (
            <Stat
              key={pool}
              label={`${pool} of $${values.size.toFixed(2)}`}
              value={`$${values.spent.toFixed(4)}`}
              color={values.left <= 0 ? 'var(--err)' : undefined}
            />
          ))}
        </StatRow>
      </section>

      {detail.stop_reason && (
        <Notice
          icon={detail.stop_reason.condition === 'ceiling' ? 'ph-arrow-line-up' : 'ph-flag-checkered'}
          tone={detail.stop_reason.condition === 'ceiling' ? 'warn' : 'dim'}
          title={`Stopped: ${detail.stop_reason.condition}`}
          body={[detail.stop_reason.detail, detail.stop_reason.recommendation].filter(Boolean).join(' — ')}
        />
      )}

      <section>
        <Label>Experiments · {detail.experiments.length}</Label>
        {detail.experiments.length === 0 ? (
          <p className="evolve-note">
            {detail.pending_experiments.length
              ? `${detail.pending_experiments.length} designed, waiting to run.`
              : 'None yet. The first round starts with a baseline on the working cases.'}
          </p>
        ) : (
          detail.experiments.map((experiment) => (
            <ExperimentRow
              key={experiment.id}
              experiment={experiment}
              claim={claims[experiment.hypothesis]?.claim}
              cases={detail.experiment_runs?.[experiment.id] ?? []}
              onOpenRun={onOpenRun}
            />
          ))
        )}
      </section>

      {detail.hypotheses.some((h) => !detail.experiments.some((e) => e.hypothesis === h.id)) && (
        <section>
          <Label>Hypotheses not tested</Label>
          {detail.hypotheses
            .filter((h) => !detail.experiments.some((e) => e.hypothesis === h.id))
            .map((h) => (
              <div className="change-row" key={h.id}>
                <code>{h.id} · {h.status}</code>
                <span className="evolve-note">{h.claim}</span>
              </div>
            ))}
        </section>
      )}

      {detail.handoffs.length > 0 && (
        <section>
          <Label>Director's findings</Label>
          {detail.handoffs.map((handoff) => (
            <div className="change-row" key={handoff.round}>
              <code>round {handoff.round} · {handoff.decision}</code>
              <ul className="research-findings">
                {handoff.findings.map((finding, index) => <li key={index}>{finding}</li>)}
              </ul>
            </div>
          ))}
        </section>
      )}

      {(detail.confirmation || detail.promotion) && (
        <section>
          <Label>Confirmation and promotion</Label>
          {detail.confirmation && (
            <p className="evolve-note">
              Sealed split: {detail.confirmation.ran
                ? `evidence ${detail.confirmation.strength} — ${detail.confirmation.success ?? ''}`
                : `not read (${detail.confirmation.reason ?? 'nothing to confirm'})`}
            </p>
          )}
          {detail.promotion && (
            <Notice
              icon="ph-sparkle"
              tone="ok"
              title={`Proposal queued (${detail.promotion.status}, ${detail.promotion.changes} change(s))`}
              body="Review the changes the program kept and apply them to this harness in Improve."
              action={
                <button className="v-btn v-btn-sm" onClick={onOpenImprove}>
                  <i className="ph ph-arrow-right" /> Open in Improve
                </button>
              }
            />
          )}
        </section>
      )}

      {detail.report && (
        <details className="research-report">
          <summary>Report</summary>
          <pre>{detail.report}</pre>
        </details>
      )}

      <details className="research-ledger">
        <summary>
          Ledger · {detail.ledger.checked} events · {detail.ledger.ok ? 'chain verified' : `broken at ${detail.ledger.broken_at}`}
        </summary>
        {[...detail.ledger_tail].reverse().map((event) => (
          <div className="research-event mono" key={event.seq}>
            <span>{when(event.ts)}</span>
            <strong>{event.kind}</strong>
            <span className="ellipsis">{summarize(event.data)}</span>
          </div>
        ))}
      </details>
    </div>
  )
}

function ContractApproval({
  contract,
  samples,
  onApprove,
}: {
  contract: ResearchContract
  samples: NonNullable<ResearchDetail['sample_cases']>
  onApprove: (contract?: string) => void
}) {
  const [editing, setEditing] = useState(false)
  const [text, setText] = useState(JSON.stringify(contract, null, 2))
  return (
    <section className="research-new">
      <Label>Approve the evaluation contract</Label>
      <p className="evolve-note">
        The director turned your concepts into these criteria. Everything the program later
        calls better is measured against them — read the sample cases: they are where a
        misunderstanding shows. Nothing is spent on changes until you approve.
      </p>
      {contract.criteria.map((criterion) => (
        <div className="change-row" key={criterion.id}>
          <code>{criterion.id}</code>{' '}
          <span className="mono research-measure">
            {criterion.check.kind}
            {criterion.check.field ? ` ${criterion.check.field}` : ''}
            {criterion.check.pattern ? ` /${criterion.check.pattern}/` : ''}
            {contract.goal_thresholds[criterion.id] !== undefined
              ? ` · done at ${Math.round(contract.goal_thresholds[criterion.id] * 100)}%` : ''}
          </span>
          <div className="evolve-note">{criterion.says}</div>
          {criterion.check.rubric && <pre>{criterion.check.rubric}</pre>}
        </div>
      ))}
      <Label>Sample cases · {samples.length}</Label>
      {samples.map((sample) => (
        <div className="research-event research-sample mono" key={sample.id}>
          <span className="ellipsis" title={sample.input}>{sample.input}</span>
          <strong>{sample.criteria.join(', ')}</strong>
          <span className="ellipsis">{Object.keys(sample.expected).length ? JSON.stringify(sample.expected) : sample.provenance}</span>
        </div>
      ))}
      {editing && (
        <textarea
          className="research-charter mono"
          spellCheck={false}
          rows={Math.min(28, text.split('\n').length + 1)}
          value={text}
          onChange={(event) => setText(event.target.value)}
        />
      )}
      <div className="proposal-actions">
        <button className="v-btn v-btn-ghost" onClick={() => setEditing((value) => !value)}>
          <i className="ph ph-pencil-simple" /> {editing ? 'Discard edits' : 'Edit'}
        </button>
        <button className="v-btn v-btn-primary" onClick={() => onApprove(editing ? text : undefined)}>
          <i className="ph ph-check" /> Approve{editing ? ' edited contract' : ''}
        </button>
      </div>
    </section>
  )
}

function QuestionCard({
  question,
  onAnswer,
}: {
  question: ResearchQuestion
  onAnswer: (questionId: string, answer: string) => void
}) {
  const [text, setText] = useState('')
  const label = question.kind === 'label' || question.kind === 'audit'
  return (
    <div className="change-row research-experiment">
      <div className="research-experiment-head">
        <code>{question.kind}{question.criterion ? ` · ${question.criterion}` : ''}</code>
        <span className="mono research-measure">
          {Object.entries(question.judges ?? {}).map(([judge, vote]) => `${judge.split('/').at(-1)}: ${vote ?? '—'}`).join(' · ')}
        </span>
      </div>
      <span>{question.text}</span>
      {question.request && <span className="mono research-measure">request: {question.request}</span>}
      {question.output !== null && question.output !== undefined && <pre>{question.output}</pre>}
      <div className="proposal-actions">
        {label ? (
          <>
            <button className="v-btn v-btn-ghost" onClick={() => onAnswer(question.id, 'fail')}>
              <i className="ph ph-x" /> Fails
            </button>
            <button className="v-btn" onClick={() => onAnswer(question.id, 'pass')}>
              <i className="ph ph-check" /> Meets it
            </button>
          </>
        ) : (
          <>
            {question.options.map((option) => (
              <button key={option} className="v-btn v-btn-ghost" onClick={() => onAnswer(question.id, option)}>
                {option}
              </button>
            ))}
            <input
              className="research-name"
              placeholder="answer in words"
              value={text}
              onChange={(event) => setText(event.target.value)}
            />
            <button className="v-btn" disabled={!text.trim()} onClick={() => onAnswer(question.id, text)}>
              Answer
            </button>
          </>
        )}
      </div>
    </div>
  )
}

function ExperimentRow({
  experiment,
  claim,
  cases,
  onOpenRun,
}: {
  experiment: ResearchExperiment
  claim?: string
  cases: NonNullable<ResearchDetail['experiment_runs']>[string]
  onOpenRun?: (runId: string) => void
}) {
  const tone = VERDICT_TONE[experiment.verdict] ?? 'var(--dim)'
  return (
    <div className="change-row research-experiment">
      <div className="research-experiment-head">
        <code>{experiment.id} · {experiment.hypothesis} → {experiment.candidate}</code>
        <span className="mono" style={{ color: tone }}>
          {experiment.verdict}
          {experiment.stopped_early ? ` (stopped: ${experiment.stopped_early})` : ''}
          {experiment.kept ? ' · kept' : ''}
        </span>
      </div>
      {claim && <span className="evolve-note">{claim}</span>}
      <span className="mono research-measure">{experiment.target_measure}</span>
      <span className="mono research-measure">{experiment.success}</span>
      {cases.length > 0 && (
        <details>
          <summary className="evolve-note">
            {cases.length} case(s), before → after · {cases.filter((c) => c.before_status !== c.after_status && c.after_status).length} changed
          </summary>
          {cases.map((row) => (
            <div className="research-event research-case mono" key={row.case}>
              <span className="ellipsis">{row.case}</span>
              <RunLink runId={row.before_run} status={row.before_status} onOpenRun={onOpenRun} />
              <span>→</span>
              <RunLink runId={row.after_run} status={row.after_status} onOpenRun={onOpenRun} />
            </div>
          ))}
        </details>
      )}
      <details>
        <summary className="evolve-note">{experiment.changes.length} change(s)</summary>
        {experiment.changes.map((change, index) => (
          <div key={index}>
            <code>{change.path}</code>
            <pre>{typeof change.value === 'string' ? change.value : JSON.stringify(change.value, null, 2)}</pre>
          </div>
        ))}
      </details>
    </div>
  )
}

function RunLink({
  runId,
  status,
  onOpenRun,
}: {
  runId: string | null
  status: string | null
  onOpenRun?: (runId: string) => void
}) {
  if (!runId) return <span className="research-measure">not run</span>
  const tone = status === 'success' ? 'var(--ok)' : 'var(--err)'
  return onOpenRun ? (
    <button className="research-runlink" style={{ color: tone }} onClick={() => onOpenRun(runId)}
      title={`Open ${runId} in Trace`}>
      {status}
    </button>
  ) : (
    <span style={{ color: tone }}>{status}</span>
  )
}

function summarize(data: Record<string, unknown>): string {
  return Object.entries(data)
    .filter(([, value]) => value !== null && value !== undefined && typeof value !== 'object')
    .map(([key, value]) => `${key}=${String(value).slice(0, 80)}`)
    .join(' · ')
}
