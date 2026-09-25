/**
 * Projection tests: `node --test` with Node's type stripping, so they need no
 * bundler, no test framework, and no dependency the workbench does not already
 * have. Run them with `npm test` in this directory.
 *
 * The cases are the ones a ledger gets quietly wrong: parallel calls, a call
 * the journal never closed, and time that must be counted once rather than
 * summed.
 */
import assert from 'node:assert/strict'
import { test } from 'node:test'

import { categoryOf, projectTrajectory } from './trajectory.ts'

type Event = {
  run_id: string
  seq: number
  timestamp: string
  type: string
  payload: Record<string, unknown>
}

const T0 = Date.parse('2026-08-25T12:00:00.000Z')

function event(seq: number, type: string, offsetMs: number, payload: Record<string, unknown> = {}): Event {
  return {
    run_id: 'run_test',
    seq,
    timestamp: new Date(T0 + offsetMs).toISOString(),
    type,
    payload,
  }
}

test('pairs a model call with its response and keeps the usage', () => {
  const trajectory = projectTrajectory([
    event(0, 'run_started', 0, { input: 'go' }),
    event(1, 'model_call', 100, { turn: 0, phase: 'act', num_messages: 1 }),
    event(2, 'model_response', 900, {
      turn: 0,
      phase: 'act',
      usage: { input_tokens: 1000, output_tokens: 40, cache_read_tokens: 12 },
      cost_usd: 0.0013,
    }),
  ] as never)

  const [span] = trajectory.spans
  assert.equal(span.lane, 'model')
  assert.equal(span.startSeq, 1)
  assert.equal(span.endSeq, 2)
  assert.equal(span.durationMs, 800)
  assert.equal(span.usage?.input_tokens, 1000)
  assert.equal(span.usage?.cache_read_tokens, 12)
  assert.equal(trajectory.totals.costUsd, 0.0013)
  // Both halves resolve to the one span, so selecting either shows the pair.
  assert.equal(trajectory.spanBySeq.get(2), span)
  assert.ok(trajectory.pairedSeqs.has(2))
})

test('pairs tool calls by their provider call id, not by adjacency', () => {
  const trajectory = projectTrajectory([
    event(1, 'tool_call', 0, { name: 'a', id: 'call_a' }),
    event(2, 'tool_call', 10, { name: 'b', id: 'call_b' }),
    // b finishes first: adjacency would attach this to a.
    event(3, 'tool_result', 200, { name: 'b', id: 'call_b', content: 'B' }),
    event(4, 'tool_result', 500, { name: 'a', id: 'call_a', content: 'A', is_error: true }),
  ] as never)

  const [a, b] = trajectory.spans
  assert.equal(a.label, 'a')
  assert.equal(a.endSeq, 4)
  assert.equal(a.durationMs, 500)
  assert.equal(a.failed, true)
  assert.equal(b.label, 'b')
  assert.equal(b.endSeq, 3)
  assert.equal(b.durationMs, 190)
  // Overlapping calls are drawn on separate rows rather than on top of each other.
  assert.notEqual(a.row, b.row)
  assert.equal(trajectory.laneRows.tool, 2)
})

test('counts overlapping tool time once', () => {
  const trajectory = projectTrajectory([
    event(1, 'tool_call', 0, { name: 'a', id: 'call_a' }),
    event(2, 'tool_call', 100, { name: 'b', id: 'call_b' }),
    event(3, 'tool_result', 400, { name: 'a', id: 'call_a' }),
    event(4, 'tool_result', 600, { name: 'b', id: 'call_b' }),
  ] as never)

  // Summing the two spans would say 900ms; the wall clock only spent 600ms.
  assert.equal(trajectory.totals.toolMs, 600)
  assert.equal(trajectory.totals.toolCalls, 2)
})

test('never invents an end for a call the journal did not close', () => {
  const trajectory = projectTrajectory([
    event(1, 'tool_call', 0, { name: 'a', id: 'call_a' }),
    event(2, 'run_finished', 500, { status: 'error' }),
  ] as never)

  const [span] = trajectory.spans
  assert.equal(span.endSeq, null)
  assert.equal(span.endMs, null)
  assert.equal(span.durationMs, null)
  // An open span contributes no measured time.
  assert.equal(trajectory.totals.toolMs, 0)
})

test('attaches updates and retries to the call they belong to', () => {
  const trajectory = projectTrajectory([
    event(1, 'tool_call', 0, { name: 'a', id: 'call_a' }),
    event(2, 'tool_update', 50, { name: 'a', id: 'call_a', text: 'working' }),
    event(3, 'tool_retry', 80, { name: 'a', id: 'call_a' }),
    event(4, 'tool_result', 200, { name: 'a', id: 'call_a' }),
  ] as never)

  const [span] = trajectory.spans
  assert.deepEqual(span.updateSeqs, [2, 3])
  // Updates are not pair halves; folding must not hide them.
  assert.ok(!trajectory.pairedSeqs.has(2))
})

test('carries the turn forward across events that do not restate it', () => {
  const trajectory = projectTrajectory([
    event(0, 'run_started', 0, {}),
    event(1, 'model_call', 10, { turn: 0 }),
    event(2, 'model_response', 20, { turn: 0 }),
    event(3, 'tool_call', 30, { name: 'a', id: 'call_a' }),
    event(4, 'tool_result', 40, { name: 'a', id: 'call_a' }),
    event(5, 'model_call', 50, { turn: 1 }),
  ] as never)

  assert.equal(trajectory.turnBySeq.get(3), 0)
  assert.equal(trajectory.turnBySeq.get(5), 1)
  assert.equal(trajectory.turnBySeq.has(0), false)
  assert.deepEqual(trajectory.turns.map((turn) => turn.turn), [0, 1])
})

test('marks a failed verifier as a point event', () => {
  const trajectory = projectTrajectory([
    event(1, 'verification_result', 100, { verifier: 'output_schema', passed: false, feedback: 'bad json' }),
  ] as never)

  const [span] = trajectory.spans
  assert.equal(span.lane, 'verify')
  assert.equal(span.durationMs, 0)
  assert.equal(span.failed, true)
  // A point event is its own start and end: it has no separate result half.
  assert.equal(span.startSeq, span.endSeq)
  assert.equal(trajectory.pairedSeqs.size, 0)
})

test('projects the four delegation events as one step per hand-off', () => {
  const trajectory = projectTrajectory([
    event(0, 'run_started', 0, { input: 'rank these' }),
    event(1, 'delegation_selected', 10, {
      mode: 'on_start',
      harness: 'ranked-retrieval',
      harness_id: 'hl-9f',
      success_rate: 0.82,
      total_runs: 44,
      cost_usd: 0.0002,
    }),
    event(2, 'delegation_started', 20, {
      mode: 'on_start',
      harness: 'ranked-retrieval',
      depth: 1,
      chain: ['hl-root', 'hl-parent'],
      cost_cap_usd: 0.25,
    }),
    event(3, 'delegation_finished', 1520, {
      mode: 'on_start',
      harness: 'ranked-retrieval',
      run_id: 'run_child',
      status: 'success',
      cost_usd: 0.031,
      turns: 3,
    }),
  ] as never)

  assert.equal(trajectory.delegations.length, 1)
  const [step] = trajectory.delegations
  assert.equal(step.phase, 'finished')
  assert.equal(step.mode, 'on_start')
  assert.equal(step.harness, 'ranked-retrieval')
  assert.equal(step.runId, 'run_child')
  assert.equal(step.status, 'success')
  // The hand-off's own cost supersedes what the selection call cost.
  assert.equal(step.costUsd, 0.031)
  assert.equal(step.turns, 3)
  assert.equal(step.successRate, 0.82)
  assert.equal(step.totalRuns, 44)
  assert.equal(step.depth, 1)
  assert.deepEqual(step.chain, ['hl-root', 'hl-parent'])
  assert.equal(step.costCapUsd, 0.25)
  assert.deepEqual(step.seqs, [1, 2, 3])
  assert.equal(step.durationMs, 1510)
  // The step is a projection beside the spans, never one of them.
  assert.equal(trajectory.spans.length, 0)
})

test('a skipped hand-off is a step of its own, with its reason', () => {
  const trajectory = projectTrajectory([
    event(1, 'delegation_skipped', 10, {
      mode: 'on_start',
      reason: 'below_fitness',
      candidates: ['scratch-harness'],
    }),
  ] as never)

  const [step] = trajectory.delegations
  assert.equal(step.phase, 'skipped')
  assert.equal(step.reason, 'below_fitness')
  assert.equal(step.harness, '')
  assert.equal(step.runId, '')
  assert.equal(step.durationMs, 0)
})

test('keeps two hand-offs to different peers apart', () => {
  const trajectory = projectTrajectory([
    event(1, 'delegation_started', 10, { mode: 'model_choice', harness: 'alpha', depth: 1 }),
    event(2, 'delegation_started', 20, { mode: 'model_choice', harness: 'beta', depth: 1 }),
    event(3, 'delegation_finished', 30, {
      mode: 'model_choice',
      harness: 'beta',
      run_id: 'run_beta',
      status: 'success',
      cost_usd: 0.01,
    }),
    event(4, 'delegation_finished', 40, {
      mode: 'model_choice',
      harness: 'alpha',
      run_id: 'run_alpha',
      status: 'verify_failed',
      cost_usd: 0.02,
    }),
  ] as never)

  assert.deepEqual(
    trajectory.delegations.map((step) => [step.harness, step.runId, step.status]),
    [
      ['alpha', 'run_alpha', 'verify_failed'],
      ['beta', 'run_beta', 'success'],
    ],
  )
})

test('a hand-off the journal never closed stays open rather than ending at zero', () => {
  const trajectory = projectTrajectory([
    event(1, 'delegation_selected', 10, { mode: 'on_verify_fail', harness: 'peer' }),
    event(2, 'delegation_started', 20, { mode: 'on_verify_fail', harness: 'peer', depth: 2 }),
  ] as never)

  const [step] = trajectory.delegations
  assert.equal(step.phase, 'started')
  assert.equal(step.endMs, null)
  assert.equal(step.durationMs, null)
})

test('a delegation event is its own category, not a tool or a run event', () => {
  assert.equal(categoryOf('delegation_selected'), 'delegation')
  assert.equal(categoryOf('delegation_started'), 'delegation')
  assert.equal(categoryOf('delegation_finished'), 'delegation')
  assert.equal(categoryOf('delegation_skipped'), 'delegation')
  assert.equal(categoryOf('tool_call'), 'tool')
  assert.equal(categoryOf('run_finished'), 'run')
})
