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

import { projectTrajectory } from './trajectory.ts'

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
