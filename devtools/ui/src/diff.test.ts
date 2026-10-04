/** A pending proposal's text change, shown as the lines that changed. */
import assert from 'node:assert/strict'
import { test } from 'node:test'

import { lineDiff } from './diff.ts'

test('an added line shows as one + line with its context, not the whole text', () => {
  const before = ['You triage tickets.', 'Steps:', '1. list', '2. read', '3. report', 'Rules:', '- be brief'].join('\n')
  const after = before.replace('- be brief', '- be brief\n- list urgent tickets first')
  const diff = lineDiff(before, after).split('\n')
  assert.deepEqual(diff.filter((line) => line.startsWith('+')), ['+- list urgent tickets first'])
  assert.ok(!diff.some((line) => line.startsWith('-')))
  assert.ok(diff.length < before.split('\n').length + 1, 'unchanged lines far from the edit are folded')
})
