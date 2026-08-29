/** Run selection and conversation normalisation. */
import assert from 'node:assert/strict'
import { test } from 'node:test'

import { autoAlias, bestRun, conversationFrom, runLabel } from './runs.ts'

const run = (over: Record<string, unknown>) => ({
  run_id: 'run_x',
  harness_name: 'h',
  harness_version_hash: 'v1',
  status: 'success',
  turns: 1,
  cost_usd: 0.01,
  duration_seconds: 1,
  started_at: '2026-08-25T10:00:00Z',
  ...over,
})

test('best run is the cheapest success, and nothing when none succeeded', () => {
  const runs = [
    run({ run_id: 'cheap', cost_usd: 0.01 }),
    run({ run_id: 'dear', cost_usd: 0.09 }),
    run({ run_id: 'failed', status: 'verify_failed', cost_usd: 0.001 }),
  ] as never

  assert.equal(bestRun(runs, 'v1')?.run_id, 'cheap')
  assert.equal(bestRun(runs, 'v-none'), null)
  assert.equal(
    bestRun([run({ run_id: 'f', status: 'error' })] as never, 'v1'),
    null,
    'a version with no success has no best run',
  )
})

/* ------------------------------------------------- thread -> conversation */

const turn = (role: string, content: string) => ({ role, content }) as never

test('a steer between a message and its reply merges rather than repeating a role', () => {
  // hiveloom refuses consecutive same-role messages, and it is right to: the
  // provider APIs do too. A steer legitimately lands next to the user message
  // it was steering, so the wire shape has to differ from the screen shape.
  const rows = conversationFrom([
    turn('user', 'summarise the deck'),
    turn('steer', 'keep Risks in'),
    turn('assistant', 'done'),
    turn('user', 'again'),
  ])

  assert.deepEqual(rows, [
    { role: 'user', content: 'summarise the deck\n\nkeep Risks in' },
    { role: 'assistant', content: 'done' },
    { role: 'user', content: 'again' },
  ])
})

test('a turn that produced no reply does not leave two user messages in a row', () => {
  const rows = conversationFrom([
    turn('user', 'first'),
    turn('assistant', ''),
    turn('user', 'second'),
  ])

  assert.deepEqual(rows, [{ role: 'user', content: 'first\n\nsecond' }])
})

test('a version boundary marker never reaches the wire', () => {
  const rows = conversationFrom([
    turn('user', 'first'),
    { role: 'assistant', content: '', versionBoundary: { from: 'a', to: 'b' } } as never,
    turn('assistant', 'reply'),
  ])

  assert.deepEqual(rows, [
    { role: 'user', content: 'first' },
    { role: 'assistant', content: 'reply' },
  ])
})

test('a conversation that opens with a reply drops it', () => {
  // Replaying a run that recorded no task of its own would otherwise start the
  // thread with an assistant message, which is not a conversation.
  const rows = conversationFrom([turn('assistant', 'orphan'), turn('user', 'go')])
  assert.deepEqual(rows, [{ role: 'user', content: 'go' }])
})

test('runLabel prefers a person\'s alias, collapsed to one line', () => {
  assert.equal(
    runLabel({ run_id: 'run_abcdef1234567890', alias: '  the hallucination\n  repro ' }),
    'the hallucination repro',
  )
})

test('runLabel falls back to the deterministic auto-alias, never the raw id or task', () => {
  const label = runLabel({ run_id: 'run_abcdef1234567890' })
  assert.match(label, /^[a-z]+-[a-z]+-abcd$/)
  assert.equal(label, autoAlias('run_abcdef1234567890'))
  assert.equal(runLabel({ run_id: 'run_abcdef1234567890', alias: '   ' }), label)
})

test('autoAlias is a pure function of the id and embeds the short hash', () => {
  assert.equal(autoAlias('run_abcdef1234567890'), autoAlias('run_abcdef1234567890'))
  assert.notEqual(autoAlias('run_abcdef1234567890'), autoAlias('run_1234567890abcdef'))
  assert.ok(autoAlias('run_e8a76671beca4641').endsWith('-e8a7'))
})
