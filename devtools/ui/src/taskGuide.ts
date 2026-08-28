import type { Attachment, HarnessDetail } from './types'

/**
 * The small amount of task affordance the workbench can faithfully derive
 * from a harness today.
 *
 * There is deliberately no second, UI-only task schema here. The description
 * and system prompt are the contract the executor receives, so the workbench
 * turns only explicit wording in that contract into guidance. Anything it
 * cannot establish stays a general task instead of being guessed.
 */
export interface TaskGuide {
  inputLabel: string
  inputHelp: string
  placeholder: string
  /** Return a reason not to spend a run, or null when the task can be sent. */
  validate: (text: string, attachments: Attachment[]) => string | null
}

export function taskGuideFor(harness: HarnessDetail): TaskGuide {
  const description = harness.description.trim()
  const prompt = stringField(harness.spec, 'system_prompt')
  const contract = `${description}\n${prompt}`.toLowerCase()

  // Only enforce this when the prompt explicitly says the task input itself
  // is a URL. Merely mentioning URLs is not enough: many research harnesses
  // accept prose instructions that happen to contain one.
  const exactUrl =
    /task input (?:is|should be) (?:the |a )?url/.test(contract) ||
    /(?:exact|single) url (?:from|in) the task input/.test(contract)

  if (exactUrl) {
    return {
      inputLabel: 'One URL',
      inputHelp: 'Paste the complete public http(s) URL. Do not add a greeting or instructions.',
      placeholder: 'https://example.com/article',
      validate: (text) =>
        isHttpUrl(text.trim())
          ? null
          : `${harness.name} expects one complete http(s) URL, not a chat message.`,
    }
  }

  const readsNamedFile =
    /(?:read|open) the file (?:named|path) in the task input/.test(contract) ||
    /task input (?:is|names|contains) (?:the |a )?(?:file|file path)/.test(contract)

  if (readsNamedFile) {
    return {
      inputLabel: 'A file',
      inputHelp: 'Attach a file with +, or enter a path already inside this harness workspace.',
      placeholder: 'Attach a file with +, or enter its workspace path',
      validate: (text, attachments) =>
        text.trim() || attachments.length > 0 ? null : 'Attach a file or enter its workspace path.',
    }
  }

  const asksQuestion = /answer the question/.test(contract)
  return {
    inputLabel: asksQuestion ? 'A question' : 'A task',
    inputHelp: description || 'Describe one representative task for this harness.',
    placeholder: asksQuestion ? 'Ask a question…' : `Give ${harness.name} a representative task…`,
    validate: (text) => (text.trim() ? null : 'Enter a task before running the harness.'),
  }
}

function stringField(record: Record<string, unknown> | undefined, key: string): string {
  const value = record?.[key]
  return typeof value === 'string' ? value : ''
}

function isHttpUrl(value: string): boolean {
  if (!value || /\s/.test(value)) return false
  try {
    const url = new URL(value)
    return (url.protocol === 'http:' || url.protocol === 'https:') && Boolean(url.hostname)
  } catch {
    return false
  }
}
