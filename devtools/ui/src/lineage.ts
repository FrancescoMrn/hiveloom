/**
 * The version graph: how a harness got from its first version to this one.
 *
 * Pure and synchronous, like `trajectory.ts` and `runs.ts`, and for the
 * same reason: this is a *projection*, not a source. Nothing here queries and
 * nothing here is stored — every node is assembled from records the workbench
 * already fetched, and each one names where it came from:
 *
 * - an **evolved** version is an applied proposal, whose `apply_result` carries
 *   the old hash, the new hash, and the YAML the gate actually let through;
 * - a **fork** version is a fork folder's `.hiveloom` lineage record, which
 *   carries the parent version, the run it re-entered, and the turn;
 * - an **edited** version is one that appears in the evidence with neither of
 *   those behind it, which is what a hand edit to `harness.yaml` looks like
 *   from the outside. It is labelled as a hand edit rather than quietly
 *   attached to a proposal that did not produce it.
 *
 * The oldest version is the **initial** one — the oldest this harness has
 * evidence for, which is not the same claim as "the first that ever existed",
 * and the note says so.
 *
 * A parent is only drawn when a record states it. Ordering the versions by time
 * and chaining each to the one before it would look identical and be a guess,
 * so an unattributable version keeps `parent: null` and sits on the trunk with
 * no edge above it.
 */
// Explicit extension: this is the one module here that imports another at
// runtime rather than for types alone, and `node --test`'s type stripping
// resolves relative specifiers verbatim.
import type { Harness, Proposal, RunRow, Stats } from './types'

export type VersionKind = 'initial' | 'evolved' | 'fork' | 'edited'

export interface VersionChange {
  path: string
  /** The previous value, when a record kept one; a proposal does not. */
  from: string | null
  to: string
}

export interface VersionNode {
  hash: string
  kind: VersionKind
  /** The version this one came from, when a record says so. */
  parent: string | null
  title: string
  /** ISO stamp of the earliest evidence for this version, or '' when none. */
  createdAt: string
  /** The folder holding this version today, when one still does. */
  folder: string
  /**
   * Every folder currently at this version, trunk first. More than one means
   * an unedited fork: a copy that has not changed anything yet, and so is not
   * a version of its own.
   */
  folders: string[]
  /** The harness id to open to run this version, when a folder still holds it. */
  harnessId: string | null
  note: string
  changes: VersionChange[]
  /** Column in the graph: 0 is the trunk, forks step right. */
  lane: number
  runs: RunRow[]
  modelPath: string
}

export interface VersionGraph {
  nodes: VersionNode[]
  /** Child → parent, by index into `nodes`. Only recorded parents appear. */
  edges: { child: number; parent: number }[]
  laneCount: number
}

/**
 * @param name        the harness these versions belong to
 * @param branches    every folder carrying that name — the trunk and its forks
 * @param runs        the harness's runs, already merged across those folders
 * @param proposals   its proposals; only applied ones produced a version
 * @param stats       the Hive's per-version run counts, which include versions
 *                    no folder holds any more
 */
export function buildVersionGraph(
  name: string,
  branches: Harness[],
  runs: RunRow[],
  proposals: Proposal[],
  stats: Stats | null,
): VersionGraph {
  const runsByVersion = new Map<string, RunRow[]>()
  for (const run of runs) {
    const bucket = runsByVersion.get(run.harness_version_hash)
    if (bucket) bucket.push(run)
    else runsByVersion.set(run.harness_version_hash, [run])
  }

  const known = new Set<string>()
  const remember = (hash: string | null | undefined) => {
    if (hash) known.add(hash)
  }

  for (const entry of stats?.versions ?? []) remember(entry.version)
  for (const run of runs) remember(run.harness_version_hash)
  for (const branch of branches) {
    remember(branch.version_hash)
    remember(branch.fork?.harness_version_hash)
    remember(branch.fork?.parent_harness_version_hash)
  }

  // Only an *applied* proposal produced a version; a pending or rejected one
  // describes a version that does not exist and must not appear in the graph.
  const applied = proposals
    .map((proposal) => ({ proposal, result: appliedResult(proposal) }))
    .filter((row): row is { proposal: Proposal; result: AppliedShape } => row.result !== null)
  for (const { result } of applied) {
    remember(result.old_version_hash)
    remember(result.new_version_hash)
  }

  const evolvedBy = new Map<string, { proposal: Proposal; result: AppliedShape }>()
  for (const row of applied) evolvedBy.set(row.result.new_version_hash, row)

  // A fork only becomes a *version* once it diverges. Copying a folder and
  // changing nothing produces an identical spec and therefore an identical
  // hash, so an unedited fork is the same version seen from a second folder —
  // giving it a node of its own would draw a version branching off itself.
  const forkedBy = new Map<string, Harness>()
  for (const branch of branches) {
    const record = branch.fork
    const hash = record?.harness_version_hash ?? (record ? branch.version_hash : undefined)
    if (!record || !hash) continue
    if (hash === record.parent_harness_version_hash) continue
    forkedBy.set(hash, branch)
  }

  const folderOf = new Map<string, Harness>()
  const foldersAt = new Map<string, string[]>()
  for (const branch of branches) {
    if (!branch.version_hash) continue
    const held = folderOf.get(branch.version_hash)
    // Two folders at one version is what an unedited fork is. The trunk wins:
    // running the fork's folder would resume its context instead.
    if (!held || (held.fork && !branch.fork)) folderOf.set(branch.version_hash, branch)
    const list = foldersAt.get(branch.version_hash)
    if (list) list.push(branch.folder)
    else foldersAt.set(branch.version_hash, [branch.folder])
  }

  const draft = [...known].map((hash) => {
    const own = runsByVersion.get(hash) ?? []
    const evolved = evolvedBy.get(hash)
    const fork = forkedBy.get(hash)
    const folder = folderOf.get(hash)
    return {
      hash,
      createdAt: earliest([
        ...own.map((run) => run.started_at ?? ''),
        evolved?.proposal.resolved_at ?? '',
        fork?.fork?.created_at ?? '',
      ]),
      runs: own,
      evolved,
      fork,
      folder,
    }
  })

  // Newest first, the way the rail and every other list here reads. A version
  // with no evidence at all has no stamp; it sorts last rather than to 1970.
  draft.sort((a, b) => {
    const left = Date.parse(a.createdAt || '')
    const right = Date.parse(b.createdAt || '')
    if (!Number.isFinite(left) && !Number.isFinite(right)) return a.hash.localeCompare(b.hash)
    if (!Number.isFinite(left)) return 1
    if (!Number.isFinite(right)) return -1
    return right - left
  })

  const oldest = draft[draft.length - 1]?.hash ?? null
  const nodes: VersionNode[] = draft.map((row) => {
    const { hash, evolved, fork, folder } = row
    const common = {
      hash,
      createdAt: row.createdAt,
      folder: folder?.folder ?? '',
      folders: foldersAt.get(hash) ?? [],
      harnessId: folder?.id ?? null,
      runs: row.runs,
      modelPath: modelPathOf(row.runs),
      lane: 0,
    }

    if (fork?.fork) {
      const record = fork.fork
      return {
        ...common,
        kind: 'fork' as const,
        parent: record.parent_harness_version_hash ?? null,
        title: `Fork at turn ${record.at_turn} · ${fork.folder}`,
        note:
          `Re-entered ${record.parent_run_id} at seq ${record.at_seq} — turn ${record.at_turn} — ` +
          'in a folder of its own. The parent run is left exactly as recorded.',
        changes: record.model_override
          ? [{ path: 'model.id', from: null, to: record.model_override }]
          : [],
      }
    }

    if (evolved) {
      const { proposal, result } = evolved
      return {
        ...common,
        kind: 'evolved' as const,
        parent: result.old_version_hash || null,
        title: `Evolved #${result.counter} · ${firstLine(result.rationale || proposal.rationale)}`,
        note: result.rationale || proposal.proposal.rationale || proposal.rationale,
        // A proposal records the value it asked for, never the value it
        // replaced, so the "from" side is genuinely unknown here rather than
        // reconstructible — and an invented one would read as recorded.
        changes: result.applied_yaml.map((change) => ({
          path: change.path,
          from: null,
          to: renderValue(change.value),
        })),
      }
    }

    if (hash === oldest) {
      return {
        ...common,
        kind: 'initial' as const,
        parent: null,
        title: 'First version',
        note: `The oldest version ${name} has evidence for. Anything before it left no runs, proposals or forks behind.`,
        changes: [],
      }
    }

    return {
      ...common,
      kind: 'edited' as const,
      parent: null,
      title: 'Edited by hand',
      note:
        'No applied proposal and no fork produced this version, which is what a direct edit to ' +
        'harness.yaml looks like from the evidence. Its parent is not recorded, so none is drawn.',
      changes: [],
    }
  })

  const indexOf = new Map<string, number>()
  nodes.forEach((node, index) => indexOf.set(node.hash, index))

  const edges: { child: number; parent: number }[] = []
  for (const [index, node] of nodes.entries()) {
    // A version cannot descend from itself; a record that says so is wrong,
    // not a self-loop to draw.
    if (node.parent === node.hash) node.parent = null
    const parent = node.parent === null ? undefined : indexOf.get(node.parent)
    // An edge only points backwards in time. A parent that sorted *after* its
    // child would draw an upward line through the graph, which never means
    // anything true — drop the edge and keep the node.
    if (parent !== undefined && parent > index) edges.push({ child: index, parent })
  }

  assignLanes(nodes, edges)
  return { nodes, edges, laneCount: Math.max(1, ...nodes.map((node) => node.lane + 1)) }
}

/**
 * Forks leave the trunk; everything else stays on it.
 *
 * A fork's lane is the first column that no other fork is already occupying
 * across the rows it spans, so two branches that overlap in time are drawn
 * side by side instead of on top of each other.
 */
function assignLanes(nodes: VersionNode[], edges: { child: number; parent: number }[]): void {
  const spans: { lane: number; from: number; to: number }[] = []
  for (const edge of edges) {
    const node = nodes[edge.child]
    if (node.kind !== 'fork') continue
    let lane = 1
    while (
      spans.some(
        (span) => span.lane === lane && edge.child <= span.to && edge.parent >= span.from,
      )
    ) {
      lane += 1
    }
    node.lane = lane
    spans.push({ lane, from: edge.child, to: edge.parent })
  }
  // A fork whose parent is not in the graph still belongs off the trunk.
  for (const node of nodes) {
    if (node.kind === 'fork' && node.lane === 0) node.lane = 1
  }
}

/** The chain from a version back to the root, newest first. */
export function ancestryOf(graph: VersionGraph, hash: string): VersionNode[] {
  const byHash = new Map(graph.nodes.map((node) => [node.hash, node]))
  const chain: VersionNode[] = []
  const seen = new Set<string>()
  let cursor: string | null = hash
  while (cursor && !seen.has(cursor)) {
    seen.add(cursor)
    const node: VersionNode | undefined = byHash.get(cursor)
    if (!node) break
    chain.push(node)
    cursor = node.parent
  }
  return chain
}

/** The versions branched directly off this one. */
export function childrenOf(graph: VersionGraph, hash: string): VersionNode[] {
  return graph.nodes.filter((node) => node.parent === hash)
}

/* ------------------------------------------------------------------ detail */

interface AppliedShape {
  old_version_hash: string
  new_version_hash: string
  counter: number
  rationale: string
  applied_yaml: { path: string; value: unknown }[]
}

/**
 * The apply record on a proposal, when it produced a version.
 *
 * `apply_result` is typed as an open record because the server returns the
 * proposal row verbatim; this narrows it once, here, and returns null for
 * anything that did not actually change the harness — including an apply that
 * ran but changed nothing, which has no new version to place.
 */
function appliedResult(proposal: Proposal): AppliedShape | null {
  const raw = proposal.apply_result
  if (!raw || proposal.status !== 'applied') return null
  const newHash = typeof raw.new_version_hash === 'string' ? raw.new_version_hash : ''
  const oldHash = typeof raw.old_version_hash === 'string' ? raw.old_version_hash : ''
  if (!newHash || newHash === oldHash) return null
  return {
    old_version_hash: oldHash,
    new_version_hash: newHash,
    counter: typeof raw.counter === 'number' ? raw.counter : 0,
    rationale: typeof raw.rationale === 'string' ? raw.rationale : '',
    applied_yaml: Array.isArray(raw.applied_yaml)
      ? (raw.applied_yaml as { path: string; value: unknown }[])
      : [],
  }
}

/** What the runs on a version actually executed on — recorded, not declared. */
function modelPathOf(runs: RunRow[]): string {
  const paths = new Set(runs.map((run) => run.model_path).filter(Boolean) as string[])
  if (paths.size === 0) return ''
  if (paths.size === 1) return [...paths][0]
  return `${paths.size} model paths`
}

function earliest(stamps: string[]): string {
  const parsed = stamps
    .filter(Boolean)
    .map((stamp) => [stamp, Date.parse(stamp)] as const)
    .filter(([, ms]) => Number.isFinite(ms))
  if (parsed.length === 0) return ''
  return parsed.sort((a, b) => a[1] - b[1])[0][0]
}

function firstLine(text: string): string {
  const line = (text || '').split('\n').find((item) => item.trim()) ?? ''
  return line.trim().length > 90 ? `${line.trim().slice(0, 89)}…` : line.trim() || 'no rationale recorded'
}

function renderValue(value: unknown): string {
  if (typeof value === 'string') return value
  try {
    return JSON.stringify(value) ?? String(value)
  } catch {
    return String(value)
  }
}
