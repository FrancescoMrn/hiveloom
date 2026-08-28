/** Which models a picker offers: the ones a run could actually start on. */
import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  activeProviders,
  modelOptions,
  modelSelectors,
  overrideFor,
  specSelector,
  workbenchModels,
} from './models.ts'
import type { Provider } from './types.ts'

const provider = (
  name: string,
  keySet: boolean,
  ids: string[],
  over: Partial<Provider> = {},
): Provider => ({
  name,
  label: name,
  api_key_env: `${name.toUpperCase()}_API_KEY`,
  api_key_set: keySet,
  api_key_from: keySet ? 'process' : '',
  open_catalog: false,
  source: 'builtin',
  scope: 'global',
  available: true,
  ...over,
  models: ids.map((id) => ({
    id,
    label: id,
    context_window: 200000,
    input_cost_per_mtok: 1,
    output_cost_per_mtok: 5,
  })),
})

const anthropic = provider('anthropic', true, ['claude-opus-5', 'claude-sonnet-5'])
const openai = provider('openai', false, ['gpt-5'])
const empty = provider('local', true, [])
/** What another harness's extension put in the registry: usable only there. */
const demo = provider('routing_lab', true, ['qa-triage'], {
  api_key_env: '',
  source: 'harness:extensions/qa_provider.py',
  scope: 'harness',
  available: false,
})

test('a provider without a key is not offered', () => {
  assert.deepEqual(
    modelSelectors([anthropic, openai]),
    ['anthropic/claude-opus-5', 'anthropic/claude-sonnet-5'],
  )
})

test('a provider with a key but no listed model adds nothing', () => {
  assert.deepEqual(activeProviders([anthropic, empty]).map((p) => p.name), ['anthropic'])
})

test('the selector in use stays offered, key or not', () => {
  const rows = modelSelectors([anthropic, openai], 'openai/gpt-5')
  assert.ok(rows.includes('openai/gpt-5'), 'the model the run is on vanished from its own picker')
})

test('an unlisted id on an open-catalog provider survives', () => {
  const rows = modelSelectors([anthropic], 'anthropic/claude-something-new')
  assert.ok(rows.includes('anthropic/claude-something-new'))
})

test('a keyless provider is offered nowhere, even when nothing else is', () => {
  // The server reads both places a key can live — its own environment and the
  // harness's .env — so "no key" is an answer, not a gap to paper over by
  // offering everything.
  assert.deepEqual(activeProviders([openai]), [])
  assert.deepEqual(modelSelectors([openai]), [])
})

test('with no key anywhere the seat still names the model the run would use', () => {
  const rows = modelOptions([openai], 'claude/claude-haiku-4-5')
  assert.deepEqual(rows, [
    { value: 'claude/claude-haiku-4-5', label: 'claude/claude-haiku-4-5', spec: true },
  ])
})

test('the workbench offers only globally registered providers', () => {
  // A harness's extension provider is usable inside that harness; it is not
  // something the workbench can put in front of every harness.
  const inside = { ...demo, available: true }
  assert.deepEqual(workbenchModels([anthropic, inside]), [
    'anthropic/claude-opus-5',
    'anthropic/claude-sonnet-5',
  ])
})

test('another harness’s extension provider is not in this harness’s picker', () => {
  // The registry is process-global and the API loads every spec to fill the
  // rail, so routing-lab's offline demo provider is *in* the directory — it
  // just is not something any other harness could run.
  assert.deepEqual(modelSelectors([anthropic, demo]), [
    'anthropic/claude-opus-5',
    'anthropic/claude-sonnet-5',
  ])
})

test('the harness that declares it still gets it', () => {
  const inside = { ...demo, available: true }
  assert.ok(modelSelectors([anthropic, inside]).includes('routing_lab/qa-triage'))
})

test('the picker holds models and nothing else', () => {
  // A harness is not bound to a model: the spec's is one row among the usable
  // ones, marked, not a stand-in row that names no model at all.
  const rows = modelOptions([anthropic], 'anthropic/claude-opus-5')
  assert.deepEqual(
    rows.map((row) => row.value),
    ['anthropic/claude-opus-5', 'anthropic/claude-sonnet-5'],
  )
  assert.deepEqual(
    rows.filter((row) => row.spec).map((row) => row.value),
    ['anthropic/claude-opus-5'],
  )
})

test('the spec’s model is offered even where nothing else about it is usable', () => {
  // Its provider lost its key, or never had one here. It is still what the
  // run would start on, so the seat has to be able to show it.
  const rows = modelOptions([anthropic], 'openai/gpt-5')
  assert.ok(rows.some((row) => row.value === 'openai/gpt-5' && row.spec))
})

test('a shortlist narrows the seat to what was chosen', () => {
  const rows = modelOptions([anthropic], 'anthropic/claude-opus-5', '', [
    'anthropic/claude-sonnet-5',
  ])
  assert.deepEqual(
    rows.map((row) => row.value),
    ['anthropic/claude-opus-5', 'anthropic/claude-sonnet-5'],
  )
})

test('an empty shortlist is no shortlist, not an empty seat', () => {
  // A harness whose settings were never opened has to have a full seat.
  assert.equal(modelOptions([anthropic], 'anthropic/claude-opus-5', '', []).length, 2)
})

test('a shortlist cannot hide the run model or the override in force', () => {
  const rows = modelOptions([anthropic], 'anthropic/claude-opus-5', 'anthropic/claude-sonnet-5', [
    'anthropic/nothing-else',
  ])
  assert.deepEqual(
    rows.map((row) => row.value),
    ['anthropic/claude-opus-5', 'anthropic/claude-sonnet-5'],
  )
})

test('an override never hides the model the spec would go back to', () => {
  const rows = modelOptions([anthropic], 'openai/gpt-5', 'anthropic/claude-sonnet-5')
  const values = rows.map((row) => row.value)
  assert.ok(values.includes('openai/gpt-5'), 'the spec’s model was unreachable')
  assert.ok(values.includes('anthropic/claude-sonnet-5'), 'the override was not shown')
})

test('choosing the spec’s own model clears the override rather than pinning it', () => {
  // Otherwise editing the spec's model would stop reaching a workspace that
  // had merely been set back to it.
  assert.equal(overrideFor('anthropic/claude-opus-5', 'anthropic/claude-opus-5'), '')
  assert.equal(
    overrideFor('anthropic/claude-sonnet-5', 'anthropic/claude-opus-5'),
    'anthropic/claude-sonnet-5',
  )
})

test('a spec whose model cannot be read keeps one honest row', () => {
  assert.equal(specSelector({ spec: {} }), '')
  assert.deepEqual(modelOptions([anthropic], '')[0], {
    value: '',
    label: 'the model in the spec',
    spec: true,
  })
})

test('specSelector reads provider and id off the spec', () => {
  assert.equal(
    specSelector({ spec: { model: { provider: 'claude', id: 'claude-opus-5' } } }),
    'claude/claude-opus-5',
  )
})

test('an unavailable provider is not offered even where it holds a key', () => {
  // The demo provider needs no key at all, which is exactly how it slipped
  // through: keyless reads as ready, and only scope says it is not ours.
  assert.deepEqual(activeProviders([openai, demo]), [])
})
