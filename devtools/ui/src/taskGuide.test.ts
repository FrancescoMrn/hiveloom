import assert from 'node:assert/strict'
import test from 'node:test'
import { taskGuideFor } from './taskGuide.ts'
import type { HarnessDetail } from './types.ts'

function harness(overrides: Partial<HarnessDetail> = {}): HarnessDetail {
  return {
    id: 'article-extractor',
    path: '/tmp/article-extractor',
    folder: 'article-extractor',
    name: 'article-extractor',
    description: 'Fetch an article by URL.',
    ok: true,
    error: '',
    explicit: true,
    trusted: true,
    stats: null,
    yaml: '',
    yaml_path: '/tmp/article-extractor/harness.yaml',
    spec: { system_prompt: 'The task input is the URL to fetch.' },
    ...overrides,
  }
}

test('an explicit URL contract blocks chat-like input before a run', () => {
  const guide = taskGuideFor(harness())

  assert.equal(guide.inputLabel, 'One URL')
  assert.match(guide.validate('Hello', []) ?? '', /expects one complete/)
  assert.equal(guide.validate('https://example.com/article', []), null)
  assert.match(
    guide.validate('https://example.com/article please', []) ?? '',
    /expects one complete/,
  )
})

test('mentioning a URL does not invent an exact-URL contract', () => {
  const guide = taskGuideFor(
    harness({
      description: 'Research a company and cite relevant URLs.',
      spec: { system_prompt: 'Answer the research task.' },
    }),
  )

  assert.equal(guide.inputLabel, 'A task')
  assert.equal(guide.validate('Research Acme', []), null)
})

test('a named-file contract teaches the attachment path', () => {
  const guide = taskGuideFor(
    harness({
      name: 'summarizer',
      description: 'Summarize a text file.',
      spec: { system_prompt: 'Read the file named in the task input with file_read.' },
    }),
  )

  assert.equal(guide.inputLabel, 'A file')
  assert.match(guide.inputHelp, /Attach a file/)
})
