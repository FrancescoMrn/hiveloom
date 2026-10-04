/** Shapes returned by devtools/ui/server.py. */

export interface Stats {
  harness_name: string
  total_runs: number
  success_rate: number
  avg_cost_usd: number
  avg_turns: number
  versions: { version: string; runs: number; success_rate: number }[]
  failure_signatures: {
    verdicts?: { feedback: string; count: number; example: string }[]
  }
}

export interface ForkRecord {
  parent_run_id: string
  parent_harness_version_hash?: string
  at_seq: number
  at_turn: number
  created_at: string
  harness_version_hash?: string
  model_override?: string | null
}

export interface Harness {
  id: string
  path: string
  /** Research programs that need the person, or are running; absent when none. */
  research?: { running: number; awaiting: number; questions: number; blocked: number } | null
  /** The directory's own name — what tells a fork from the parent it copied. */
  folder: string
  /** Where this folder was forked from, when it was. */
  fork?: ForkRecord | null
  /**
   * The harness folder this one lives in: its own path for a trunk, the
   * containing harness for a fork under `.hiveloom/forks`. Containment is a
   * fact about the path, so it holds even when a fork's spec was renamed.
   */
  root_path?: string
  /** True when this row is a fork contained by another harness. */
  is_fork?: boolean
  /** Id of the containing harness, when that harness is in the catalog too. */
  parent_id?: string
  /** The version this folder is at right now. */
  version_hash?: string
  name: string
  description: string
  ok: boolean
  error: string
  /** Offered via --dir for this process rather than registered. */
  explicit: boolean
  trusted: boolean
  stats: Stats | null
}

export interface HarnessDetail extends Harness {
  yaml: string
  yaml_path: string
  spec?: Record<string, unknown>
  /** The version a run started now would be recorded under. */
  version_hash?: string
  /** Construction-API changes (set/add/remove/set model) and the versions they linked. */
  constructions?: ConstructionStep[]
}

/** One validated construction change, from the harness's construction log. */
export interface ConstructionStep {
  timestamp: string
  command: string
  args: Record<string, unknown>
  old_version_hash: string
  new_version_hash: string
}

export interface CatalogEntry {
  name: string
  description: string
  tags: string[]
  params: { name: string; type: string; required: boolean; description?: string }[]
  source: string
}

export type Catalog = Record<string, CatalogEntry[]>

/** One line of the run stream: a trace event, or the terminating frame. */
export interface TraceEvent {
  run_id: string
  harness_name?: string
  harness_version_hash?: string
  seq: number
  timestamp: string
  type: string
  prev?: string
  payload: Record<string, unknown>
}

export interface Verdict {
  verifier: string
  passed: boolean
  feedback: string
}

/** One completed hand-off, as `run_result_payload` and `run_finished` report it. */
export interface Delegation {
  harness: string
  run_id: string
  status: string
  cost_usd: number
  turns: number
  output: string
  reason: string
}

/**
 * A peer that would have fitted but was not used.
 *
 * `reason` is one of `no_candidates`, `below_fitness`, `none_fit`,
 * `unparsable`, `depth`, `cycle`, `budget`, `listed` — see docs/delegation.md.
 */
export interface Referral {
  harness: string
  description: string
  success_rate: number
  total_runs: number
  reason: string
}

export interface RunResult {
  type: 'run_result'
  ok: boolean
  status: string
  output: string
  turns: number
  cost_usd: number
  duration_seconds: number
  run_id: string
  reason: string
  verdicts: Verdict[]
  artifacts: Artifact[]
  /**
   * What this run handed to a peer. Optional because a conversation stored
   * before delegation existed has results without it — never because the
   * server omits it.
   */
  delegations?: Delegation[]
  /** Peers that fitted but were not used, so the reader can go there directly. */
  referrals?: Referral[]
  /** The share of `cost_usd` that was spent by children, not by this run. */
  delegated_cost_usd?: number
}

export interface RunRow {
  run_id: string
  harness_name: string
  /** The input that opened the run. */
  task?: string
  /** Human name for the run, when someone set one; auto-alias otherwise. */
  alias?: string | null
  /** Set by the client when runs from several branch folders are merged. */
  folder?: string
  harness_version_hash: string
  status: string
  turns: number
  cost_usd: number
  duration_seconds: number
  started_at?: string
  finished_at?: string
  reason?: string
  trace_path?: string
  parent_run_id?: string | null
  /** How this run hangs off its parent: a delegated peer run, or a fork. */
  lineage_kind?: 'delegation' | 'fork' | null
  forked_at_seq?: number | null
  /** Set when the run belongs to a fork this harness contains: the fork's folder. */
  fork_folder?: string | null
  model_path?: string
  verifications?: Verification[]
  guardrail_triggers?: GuardrailTrigger[]
}

export interface Verification {
  seq: number
  verifier: string
  passed: number | boolean
  feedback: string
}

export interface GuardrailTrigger {
  seq: number
  guardrail: string
  kind: string
  reason: string
  hook?: string
}

export interface JournalIntegrity {
  ok: boolean
  chained: boolean
  checked: number
  broken_at: number | null
  reason: string
  summary: string
}

export interface ForkPoint {
  seq: number
  turn: number
  phase: string
  num_messages: number
  timestamp: string
}

export interface Artifact {
  kind: string
  data: unknown
  /** Tool that emitted it, present on run-result artifacts. */
  tool?: string
}

export interface CopilotInfo {
  ok: true
  name: string
  description: string
  model: string
  /** `provider/id` an unset evolution model resolves to. */
  strong_model?: string
  version_hash: string
  suggestions: string[]
}

export interface HarnessInterface {
  exists: boolean
  harness_id: string
  path?: string
  html: string
  sha256?: string
}

export interface ConversationMessage {
  role: 'user' | 'assistant'
  content: string
  attachments?: Attachment[]
  events?: TraceEvent[]
  result?: RunResult
  artifacts?: Artifact[]
  error?: string
}

export interface ConversationSelection {
  harness_id?: string
  run_id?: string
}

export interface ConversationSummary {
  id: string
  title: string
  created_at: string
  updated_at: string
  selection: ConversationSelection
  message_count: number
  preview: string
}

export interface ConversationRecord extends Omit<ConversationSummary, 'message_count' | 'preview'> {
  messages: ConversationMessage[]
}

export interface MemoryRecord {
  id: string
  scope: 'global' | 'harness'
  harness_id: string | null
  content: string
  created_at: string
  updated_at: string
}

export interface Lineage {
  run: RunRow | null
  ancestors: (RunRow | { run_id: string; missing: true })[]
  /** Historically forks only; the Hive now returns every child here. */
  forks: RunRow[]
  /** Every run started from this one, forks and delegated peer runs alike. */
  children?: RunRow[]
}

export interface RunDetail {
  run: RunRow
  events: TraceEvent[]
  integrity: JournalIntegrity | null
  lineage: Lineage
  fork_points: ForkPoint[]
  artifacts: Artifact[]
  /** Read back off the recorded `run_finished` event, like `artifacts`. */
  delegations?: Delegation[]
  referrals?: Referral[]
  delegated_cost_usd?: number
}

export interface MaterializedContext {
  run_id: string
  seq: number
  type: string
  available: boolean
  faithful: boolean
  request: {
    system: string
    messages: Record<string, unknown>[]
    tools: Record<string, unknown>[]
  }
}

export interface ProviderModel {
  id: string
  label: string
  /** From hiveloom's model registry — the same numbers a run is costed with. */
  context_window: number | null
  input_cost_per_mtok: number
  output_cost_per_mtok: number
  /** Superseded but still valid and priced: kept off pickers unless in use. */
  legacy?: boolean
  /** Access-program only (e.g. Project Glasswing): kept off pickers unless in use. */
  restricted?: boolean
}

/** One model a workbench provider offers, priced from hiveloom's registry. */
export interface WorkbenchModel {
  id: string
  label: string
  context_window: number | null
  /** null for an id the registry does not price (OpenRouter routes, custom ids). */
  input_cost_per_mtok: number | null
  output_cost_per_mtok: number | null
}

/** A provider this workbench was set up with (Settings → Models). */
export interface WorkbenchProvider {
  name: string
  label: string
  custom: boolean
  base_url: string
  api_key_env: string
  /** Whether a key is present — never the key itself. */
  key_set: boolean
  /** `workbench` (~/.hiveloom/.env), `process` (the API's environment), or ''. */
  key_from: 'workbench' | 'process' | 'none' | ''
  /** False for a key exported in the environment: it is removed there. */
  removable: boolean
  /** `catalog`: pick from hiveloom's list. `ids`: name the model ids yourself. */
  model_entry: 'catalog' | 'ids'
  models: WorkbenchModel[]
}

/** A provider that can be added: the four named ones (plus "Other" in the UI). */
export interface ProviderOffer {
  name: string
  label: string
  models: 'catalog' | 'ids'
  api_key_env: string
  choices: WorkbenchModel[]
}

export interface WorkbenchDirectory {
  catalog: ProviderOffer[]
  providers: WorkbenchProvider[]
  /** `provider/id` for every model of a provider whose key is set. */
  enabled_models: string[]
  default_model: string
  /** What a run falls back to when its harness's own model cannot run here. */
  effective_default: string
}

export interface Provider {
  name: string
  label: string
  api_key_env: string
  /** Whether the key is present — never the key itself. */
  api_key_set: boolean
  /**
   * Where it was found, in the order a run resolves them: `process` (the
   * environment the API was started in), `workbench` (`~/.hiveloom/.env`, the
   * keys this machine develops with), `harness` (the open harness's own
   * `.env`, which travels with it), `none` (this provider needs no key), or ''
   * for not found.
   */
  api_key_from: 'process' | 'workbench' | 'harness' | 'none' | ''
  open_catalog: boolean
  /** Where it was registered: `builtin`, a pack, or `harness:<ext ref>`. */
  source: string
  /**
   * `harness` means one harness's extension registered it. The registry is
   * process-global and listing the rail loads every spec, so those providers
   * are visible to the API without being usable by anyone else.
   */
  scope: 'global' | 'harness'
  /** Whether the harness this directory was asked about may pick it. */
  available: boolean
  models: ProviderModel[]
}

export interface ForkResult {
  ok: true
  directory: string
  /** The fork's id in the rail: it is registered as it is created. */
  harness_id: string
  parent_run_id: string
  at_seq: number
  turn: number
  messages: number
  version_hash: string
  /**
   * The executor this fork was moved onto, or null when it kept the parent's.
   * An object, not a string: the "from" side is what makes the A/B legible,
   * and rendering the whole record as a React child is what broke before.
   */
  model_override: ForkModelOverride | null
  trust_inherited: boolean
  warnings: string[]
}

export interface ForkModelOverride {
  /** `provider:model` the parent ran on. */
  from: string
  provider: string
  model: string
}

/* ------------------------------------------------------------ evolution */

export interface YamlChange {
  path: string
  value: unknown
  rationale: string
}

export interface CodeChange {
  file: string
  source: string
  rationale: string
}

export interface Proposal {
  id: string
  harness_name: string
  spec_version_hash: string
  status: string
  trigger: string
  rationale: string
  created_at: string
  resolved_at: string | null
  proposal: { rationale: string; yaml_changes: YamlChange[]; code_changes: CodeChange[] }
  /** What the gate allowed: frozen fields never reach `accepted`. */
  gate: { accepted: YamlChange[]; rejected: { path: string; reason: string }[]; code_changes: CodeChange[] }
  apply_result: Record<string, unknown> | null
  /** For an applied proposal: the evolution it produced, with its spec diff. */
  evolution?: {
    counter: number
    old_version_hash: string
    new_version_hash: string
    created_at: string
    /** Unified diff of harness.yaml, before → after (capped when recorded). */
    yaml_diff: string
  } | null
}

export interface ApplyResult {
  ok: true
  changed: boolean
  old_version_hash: string
  new_version_hash: string
  counter: number
  rationale: string
  applied_yaml: YamlChange[]
  rejected: { path: string; reason: string }[]
  applied_code: string[]
  pending_code?: string[]
}

/* ----------------------------------------------------------- comparison */

export interface VersionSide {
  version: string
  runs: number
  successes: number
  success_rate: number
  avg_cost_usd: number
  avg_turns: number
  swapped_runs: number
  failures: { verdicts?: { feedback: string; count: number }[] }
}

export interface Comparison {
  harness_name: string
  left: VersionSide
  right: VersionSide
  delta: { success_rate: number; avg_cost_usd: number; avg_turns: number; runs: number }
  fixed_failures: string[]
  new_failures: string[]
  /** Fewer than five runs on a side: the delta is not evidence yet. */
  underpowered: boolean
}

/* ---------------------------------------------------------- workbench state */

/** Version hash → the label a person gave it, per harness folder. */
export type VersionTags = Record<string, string>

/**
 * A file handed to a turn.
 *
 * `path` is the point: the server wrote the bytes into the harness's own
 * workspace, so the message names a path the harness can open with `file_read`
 * and the journal records something that still resolves later.
 */
export interface Attachment {
  name: string
  path: string
  bytes: number
  sha256: string
}

export interface ApiError {
  code: string
  message: string
  detail?: string
  path?: string
}

export class HiveloomApiError extends Error {
  constructor(readonly info: ApiError) {
    super(info.message)
  }

  /** The harness needs an explicit trust decision — an action, not a failure. */
  get needsTrust(): boolean {
    return this.info.code === 'trust_required'
  }
}

/** One research program of a harness, as the rail of the Research view lists it. */
export interface ResearchProgramRow {
  name: string
  status: string
  unit: string
  round: number
  incumbent: string
  started_at: string
  updated_at: string
  running: boolean
}

export interface ResearchJob {
  running: boolean
  until: 'unit' | 'round' | 'done'
  started_at: string
  finished_at: string | null
  error: string | null
  detail?: string
  steps: { unit: string; outcome?: string }[]
}

export interface ResearchExperiment {
  id: string
  round: number
  hypothesis: string
  candidate: string
  base: string
  changes: { path: string; value: unknown; rationale?: string }[]
  verdict: string
  kept: boolean
  stopped_early: string | null
  guard_ok: boolean
  measured_effect: number
  success_gain?: number
  success: string
  target_measure: string
  summary: string
}

export interface ResearchHypothesis {
  id: string
  round: number
  claim: string
  levers: string[]
  target: string
  expect: 'increase' | 'decrease'
  by: number | null
  prior: number
  falsifier: string
  status: string
}

export interface ResearchDetail {
  name: string
  status: string
  unit: string
  round: number
  incumbent: string
  goal: string
  charter: Record<string, unknown> & { levers: string[]; models: { director: string } }
  split: { working: number; holdout: number }
  budget: Record<string, { size: number; spent: number; left: number }>
  hypotheses: ResearchHypothesis[]
  experiments: ResearchExperiment[]
  pending_experiments: string[]
  calibration: { hypothesis: string; predicted: number; measured: number; gap: number }[]
  handoffs: { round: number; findings: string[]; next_focus: string[]; decision: string; stop_reason: string | null }[]
  stop_reason: { condition: string; detail: string; recommendation?: string } | null
  confirmation: { ran: boolean; strength: string | null; reason?: string; success?: string } | null
  promotion: { proposal_id: string; status: string; strength: string; changes: number } | null
  ledger: { ok: boolean; checked: number; broken_at: number | null }
  report: string | null
  ledger_tail: { seq: number; ts: string; kind: string; data: Record<string, unknown> }[]
  job: ResearchJob | null
  progress?: { purpose: string | null; unit: string; completed: number; total: number } | null
  mode?: 'eval' | 'concepts'
  awaiting?: 'contract' | null
  blocked_reason?: string | null
  contract?: ResearchContract | null
  contract_version?: number | null
  draft_contract?: ResearchContract | null
  sample_cases?: { id: string; input: string; expected: Record<string, unknown>; criteria: string[]; provenance: string }[] | null
  trust?: { criterion: string; measured: boolean; how: string; kappa?: number | null; anchors?: number }[]
  question_list?: ResearchQuestion[]
  working_cases?: number
  experiment_runs?: Record<string, {
    case: string
    before_run: string | null
    before_status: string | null
    after_run: string | null
    after_status: string | null
  }[]>
}

export interface ResearchContract {
  criteria: { id: string; says: string; check: { kind: string; field?: string | null; pattern?: string | null; rubric?: string | null }; weight?: number }[]
  goal_thresholds: Record<string, number>
}

export interface ResearchQuestion {
  id: string
  kind: 'label' | 'audit' | 'disambiguate' | 'confirm'
  text: string
  criterion: string | null
  options: string[]
  request: string | null
  output: string | null
  judges: Record<string, string | null>
  asked_by: 'engine' | 'director'
  status: 'open' | 'answered' | 'withdrawn'
  answer: string | null
}
