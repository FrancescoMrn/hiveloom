/**
 * The workbench's provider directory, shared by every component that offers a
 * model: Settings, the composer, the harness workspace, the fork dialog.
 *
 * One copy, published after every change, so adding a provider in Settings
 * puts its models in the composer without a reload — and no component holds a
 * stale list of what can run.
 */
import { useEffect, useState } from 'react'
import { api } from './api'
import type { WorkbenchDirectory } from './types'

const EMPTY: WorkbenchDirectory = {
  catalog: [],
  providers: [],
  enabled_models: [],
  default_model: '',
  effective_default: '',
}

let current: WorkbenchDirectory | null = null
let inflight: Promise<void> | null = null
const listeners = new Set<(directory: WorkbenchDirectory) => void>()

export function publishDirectory(directory: WorkbenchDirectory): void {
  current = directory
  for (const listener of listeners) listener(directory)
}

export function refreshDirectory(): Promise<void> {
  inflight ??= api
    .workbenchProviders()
    .then(publishDirectory)
    .catch(() => publishDirectory(current ?? EMPTY))
    .finally(() => {
      inflight = null
    })
  return inflight
}

export function useWorkbenchDirectory(): WorkbenchDirectory | null {
  const [directory, setDirectory] = useState(current)
  useEffect(() => {
    listeners.add(setDirectory)
    if (current) setDirectory(current)
    else void refreshDirectory()
    return () => {
      listeners.delete(setDirectory)
    }
  }, [])
  return directory
}

/** Whether `selector`'s provider is set up with a key on this machine. */
export function canRun(directory: WorkbenchDirectory | null, selector: string): boolean {
  const provider = selector.split('/')[0]
  return !!directory?.providers.some((row) => row.name === provider && row.key_set)
}

/** `Other` providers get a name derived from the label the person typed. */
export function providerSlug(label: string): string {
  const slug = label
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^[^a-z]+/, '')
    .replace(/_+$/, '')
    .slice(0, 32)
  return slug.length >= 2 ? slug : ''
}
