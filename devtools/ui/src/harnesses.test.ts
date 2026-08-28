/** Grouping the catalog: a harness and the forks kept inside it. */
import assert from 'node:assert/strict'
import { test } from 'node:test'

import { groupHarnesses, groupLabel } from './harnesses.ts'
import type { Harness } from './types.ts'

const harness = (over: Partial<Harness> & { id: string; path: string }): Harness => ({
  folder: over.path.split('/').pop() ?? '',
  name: 'example-summarizer',
  description: '',
  ok: true,
  error: '',
  explicit: false,
  trusted: true,
  stats: null,
  ...over,
})

const trunk = harness({ id: 'summarizer', path: '/h/summarizer' })
const fork = harness({
  id: 'probe',
  path: '/h/summarizer/.hiveloom/forks/probe',
  root_path: '/h/summarizer',
  is_fork: true,
  parent_id: 'summarizer',
})

test('a fork is grouped under the harness that contains it', () => {
  const groups = groupHarnesses([trunk, fork])

  assert.equal(groups.length, 1)
  assert.equal(groups[0].trunk.id, 'summarizer')
  assert.deepEqual(groups[0].forks.map((f) => f.id), ['probe'])
})

test('the harness is the trunk however the catalog is ordered', () => {
  for (const order of [[trunk, fork], [fork, trunk]]) {
    const [group] = groupHarnesses(order)
    assert.equal(group.trunk.id, 'summarizer', 'a fork took its harness’s place')
  }
})

test('renaming a fork does not scatter it out of its harness', () => {
  // Grouping by name would put this in a group of its own; containment does not.
  const renamed = { ...fork, name: 'something-else-entirely' }

  const groups = groupHarnesses([trunk, renamed])

  assert.equal(groups.length, 1)
  assert.deepEqual(groups[0].forks.map((f) => f.id), ['probe'])
})

test('two unrelated harnesses that share a name stay two harnesses', () => {
  const other = harness({ id: 'other', path: '/elsewhere/summarizer' })

  const groups = groupHarnesses([trunk, other])

  assert.deepEqual(groups.map((g) => g.trunk.id).sort(), ['other', 'summarizer'])
})

test('a fork whose harness is not listed is a row of its own', () => {
  const groups = groupHarnesses([fork])

  assert.equal(groups.length, 1)
  assert.equal(groups[0].trunk.id, 'probe')
  assert.deepEqual(groups[0].forks, [])
})

test('a harness that will not load is labelled by its folder', () => {
  const broken = harness({ id: 'b', path: '/h/broken', ok: false, name: '' })

  assert.equal(groupLabel(groupHarnesses([broken])[0]), 'broken')
})
