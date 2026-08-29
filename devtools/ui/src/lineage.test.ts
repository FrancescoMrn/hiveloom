/**
 * Version-graph tests.
 *
 * The cases here are the ones a lineage view gets quietly wrong: attributing a
 * version to a proposal that was never applied, inventing a parent for a hand
 * edit because it happens to sit next to one in time, drawing an edge that
 * points forward in time, and stacking two forks in the same column because
 * their lifetimes overlap.
 */
import assert from 'node:assert/strict'
import { test } from 'node:test'

import { ancestryOf, buildVersionGraph, childrenOf } from './lineage.ts'

const run = (over: Record<string, unknown>) => ({
  run_id: 'run_x',
  harness_name: 'summarizer',
  harness_version_hash: 'v1',
  status: 'success',
  turns: 1,
  cost_usd: 0.01,
  duration_seconds: 1,
  started_at: '2026-08-25T10:00:00Z',
  ...over,
})

const trunk = (over: Record<string, unknown> = {}) => ({
  id: 'summarizer',
  path: '/x/summarizer',
  folder: 'summarizer',
  name: 'summarizer',
  description: '',
  ok: true,
  error: '',
  explicit: false,
  trusted: true,
  stats: null,
  version_hash: 'v3',
  ...over,
})

const proposal = (over: Record<string, unknown>) => ({
  id: 'p1',
  harness_name: 'summarizer',
  spec_version_hash: 'v1',
  status: 'applied',
  trigger: 'failures',
  rationale: 'raise the ceiling',
  created_at: '2026-08-25T09:00:00Z',
  resolved_at: '2026-08-25T09:30:00Z',
  proposal: { rationale: 'raise the ceiling', yaml_changes: [], code_changes: [] },
  gate: { accepted: [], rejected: [], code_changes: [] },
  apply_result: null,
  ...over,
})

const build = (
  branches: unknown[],
  runs: unknown[],
  proposals: unknown[] = [],
  stats: unknown = null,
) => buildVersionGraph('summarizer', branches as never, runs as never, proposals as never, stats as never)

test('an applied proposal names the version it produced and what it changed', () => {
  const graph = build(
    [trunk({ version_hash: 'v2' })],
    [
      run({ run_id: 'r1', harness_version_hash: 'v1', started_at: '2026-08-25T08:00:00Z' }),
      run({ run_id: 'r2', harness_version_hash: 'v2', started_at: '2026-08-25T10:00:00Z' }),
    ],
    [
      proposal({
        apply_result: {
          old_version_hash: 'v1',
          new_version_hash: 'v2',
          counter: 3,
          rationale: 'raise the word ceiling',
          applied_yaml: [{ path: 'validators.length_check.max_words', value: 450 }],
        },
      }),
    ],
  )

  const [newest, oldest] = graph.nodes
  assert.equal(newest.hash, 'v2')
  assert.equal(newest.kind, 'evolved')
  assert.equal(newest.parent, 'v1')
  assert.equal(newest.title, 'Evolved #3 · raise the word ceiling')
  assert.deepEqual(newest.changes, [
    { path: 'validators.length_check.max_words', from: null, to: '450' },
  ])
  assert.equal(oldest.kind, 'initial')
  assert.deepEqual(graph.edges, [{ child: 0, parent: 1 }])
})

test('a proposal that was never applied produces no version', () => {
  const graph = build(
    [trunk({ version_hash: 'v1' })],
    [run({ harness_version_hash: 'v1' })],
    [
      proposal({
        status: 'pending',
        apply_result: { old_version_hash: 'v1', new_version_hash: 'v2', applied_yaml: [] },
      }),
    ],
  )

  assert.deepEqual(graph.nodes.map((node) => node.hash), ['v1'])
})

test('an apply that changed nothing does not fabricate a node', () => {
  const graph = build(
    [trunk({ version_hash: 'v1' })],
    [run({ harness_version_hash: 'v1' })],
    [
      proposal({
        apply_result: {
          old_version_hash: 'v1',
          new_version_hash: 'v1',
          counter: 1,
          rationale: '',
          applied_yaml: [],
        },
      }),
    ],
  )

  assert.deepEqual(graph.nodes.map((node) => node.hash), ['v1'])
})

test('a version with no record behind it is a hand edit, not a guessed child', () => {
  const graph = build(
    [trunk({ version_hash: 'v2' })],
    [
      run({ run_id: 'r1', harness_version_hash: 'v1', started_at: '2026-08-25T08:00:00Z' }),
      run({ run_id: 'r2', harness_version_hash: 'v2', started_at: '2026-08-25T10:00:00Z' }),
    ],
  )

  const [newest] = graph.nodes
  assert.equal(newest.hash, 'v2')
  assert.equal(newest.kind, 'edited')
  // Ordering by time and chaining would look identical and be a guess.
  assert.equal(newest.parent, null)
  assert.equal(graph.edges.length, 0)
})

test('a fork folder becomes a fork node off its recorded parent version', () => {
  const graph = build(
    [
      trunk({ version_hash: 'v1' }),
      trunk({
        id: 'a91f-turn2',
        folder: 'a91f-turn2',
        version_hash: 'f1',
        fork: {
          parent_run_id: 'run_a91f',
          parent_harness_version_hash: 'v1',
          at_seq: 7,
          at_turn: 2,
          created_at: '2026-08-26T11:40:00Z',
          harness_version_hash: 'f1',
          model_override: 'claude-opus-4-1',
        },
      }),
    ],
    [run({ harness_version_hash: 'v1', started_at: '2026-08-25T08:00:00Z' })],
  )

  const fork = graph.nodes.find((node) => node.hash === 'f1')
  assert.ok(fork)
  assert.equal(fork.kind, 'fork')
  assert.equal(fork.parent, 'v1')
  assert.equal(fork.folder, 'a91f-turn2')
  assert.equal(fork.harnessId, 'a91f-turn2')
  assert.deepEqual(fork.changes, [{ path: 'model.id', from: null, to: 'claude-opus-4-1' }])
  // Forks leave the trunk, so they never share its column.
  assert.equal(fork.lane, 1)
  assert.equal(graph.laneCount, 2)
})

test('two forks whose lifetimes overlap get columns of their own', () => {
  const forkOf = (id: string, hash: string, at: string) =>
    trunk({
      id,
      folder: id,
      version_hash: hash,
      fork: {
        parent_run_id: 'run_a',
        parent_harness_version_hash: 'v1',
        at_seq: 3,
        at_turn: 1,
        created_at: at,
        harness_version_hash: hash,
      },
    })

  const graph = build(
    [trunk({ version_hash: 'v1' }), forkOf('fa', 'f1', '2026-08-26T12:00:00Z'), forkOf('fb', 'f2', '2026-08-26T11:00:00Z')],
    [run({ harness_version_hash: 'v1', started_at: '2026-08-25T08:00:00Z' })],
  )

  const lanes = graph.nodes.filter((node) => node.kind === 'fork').map((node) => node.lane)
  assert.deepEqual([...lanes].sort(), [1, 2])
})

test('an edge whose parent is newer than its child is dropped, not drawn backwards', () => {
  // Contradictory history: the record claims v1 came from v2, but every scrap
  // of evidence for v2 is newer than v1's, so drawing the edge would run a
  // line up the page. The node survives; the claim does not.
  const graph = build(
    [trunk({ version_hash: 'v2' })],
    [
      run({ run_id: 'r1', harness_version_hash: 'v1', started_at: '2026-08-25T08:00:00Z' }),
      run({ run_id: 'r2', harness_version_hash: 'v2', started_at: '2026-08-25T12:00:00Z' }),
    ],
    [
      proposal({
        resolved_at: null,
        apply_result: {
          old_version_hash: 'v2',
          new_version_hash: 'v1',
          counter: 1,
          rationale: 'out of order',
          applied_yaml: [],
        },
      }),
    ],
  )

  assert.deepEqual(graph.nodes.map((node) => node.hash), ['v2', 'v1'])
  assert.equal(graph.nodes[1].parent, 'v2')
  assert.equal(graph.edges.length, 0)
})

test('the version on disk appears even with no runs behind it', () => {
  const graph = build([trunk({ version_hash: 'fresh' })], [])
  assert.deepEqual(graph.nodes.map((node) => node.hash), ['fresh'])
  assert.equal(graph.nodes[0].kind, 'initial')
  assert.equal(graph.nodes[0].runs.length, 0)
})

test('runs land on the version they were recorded against', () => {
  const graph = build(
    [trunk({ version_hash: 'v2' })],
    [
      run({ run_id: 'r1', harness_version_hash: 'v1', task: 'first' }),
      run({
        run_id: 'r2',
        harness_version_hash: 'v2',
        task: 'second',
        started_at: '2026-08-25T11:00:00Z',
        model_path: 'claude/claude-sonnet-4-5',
      }),
    ],
  )

  const v2 = graph.nodes.find((node) => node.hash === 'v2')
  const v1 = graph.nodes.find((node) => node.hash === 'v1')
  assert.deepEqual(v2?.runs.map((run) => run.run_id), ['r2'])
  assert.deepEqual(v1?.runs.map((run) => run.run_id), ['r1'])
  assert.equal(v2?.modelPath, 'claude/claude-sonnet-4-5')
  // Nothing was recorded for v1's model path, and nothing is claimed.
  assert.equal(v1?.modelPath, '')
})

test('ancestry walks recorded parents only, and stops rather than looping', () => {
  const graph = build(
    [trunk({ version_hash: 'v3' })],
    [
      run({ run_id: 'r1', harness_version_hash: 'v1', started_at: '2026-08-25T08:00:00Z' }),
      run({ run_id: 'r2', harness_version_hash: 'v2', started_at: '2026-08-25T09:00:00Z' }),
      run({ run_id: 'r3', harness_version_hash: 'v3', started_at: '2026-08-25T10:00:00Z' }),
    ],
    [
      proposal({
        id: 'p1',
        apply_result: {
          old_version_hash: 'v1',
          new_version_hash: 'v2',
          counter: 1,
          rationale: 'one',
          applied_yaml: [],
        },
      }),
      proposal({
        id: 'p2',
        apply_result: {
          old_version_hash: 'v2',
          new_version_hash: 'v3',
          counter: 2,
          rationale: 'two',
          applied_yaml: [],
        },
      }),
    ],
  )

  assert.deepEqual(ancestryOf(graph, 'v3').map((node) => node.hash), ['v3', 'v2', 'v1'])
  assert.deepEqual(childrenOf(graph, 'v2').map((node) => node.hash), ['v3'])
})

test('a fork that changed nothing is a second folder, not a second version', () => {
  // Copying a folder and editing nothing produces an identical spec, and so an
  // identical hash. Giving it a node would draw a version branching off itself.
  const graph = build(
    [
      trunk({ version_hash: 'v1' }),
      trunk({
        id: 'a91f-turn2',
        folder: 'a91f-turn2',
        version_hash: 'v1',
        fork: {
          parent_run_id: 'run_a91f',
          parent_harness_version_hash: 'v1',
          at_seq: 7,
          at_turn: 2,
          created_at: '2026-08-26T11:40:00Z',
          harness_version_hash: 'v1',
        },
      }),
    ],
    [run({ harness_version_hash: 'v1' })],
  )

  assert.deepEqual(graph.nodes.map((node) => node.hash), ['v1'])
  assert.equal(graph.nodes[0].kind, 'initial')
  assert.equal(graph.nodes[0].parent, null)
  assert.equal(graph.edges.length, 0)
  // The second folder is not lost — it is reported as what it is.
  assert.deepEqual(graph.nodes[0].folders.sort(), ['a91f-turn2', 'summarizer'])
  // The trunk wins the folder that a new run would run.
  assert.equal(graph.nodes[0].folder, 'summarizer')
})
