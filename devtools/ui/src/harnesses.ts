/** Grouping the catalog into what the rail actually shows. */
import type { Harness } from './types'

export interface HarnessGroup {
  /** Stable key for the group: the containing folder's path. */
  key: string
  /** The harness the group is about. */
  trunk: Harness
  /** The forks contained in it, in catalog order. */
  forks: Harness[]
}

/**
 * One group per harness, with the forks kept inside it.
 *
 * Grouping follows *containment* — `root_path`, the folder a fork lives under
 * — rather than the harness name. A fork starts out sharing its parent's name,
 * which makes the name look like the obvious key, but renaming a fork is an
 * ordinary spec edit and would then scatter it out of its harness; and two
 * unrelated harnesses that happen to share a name are two harnesses, not one
 * with a phantom branch. The path says who contains whom and nothing else can.
 *
 * A fork whose harness is not in the catalog — registered on its own, or its
 * harness unregistered since — becomes a group of its own rather than
 * disappearing into a parent that is not there to be shown.
 */
export function groupHarnesses(items: Harness[]): HarnessGroup[] {
  const paths = new Set(items.map((item) => item.path))
  const groups = new Map<string, Harness[]>()
  for (const item of items) {
    const root = item.root_path ?? item.path
    const key = paths.has(root) ? root : item.path
    const bucket = groups.get(key)
    if (bucket) bucket.push(item)
    else groups.set(key, [item])
  }

  return [...groups.entries()].map(([key, branches]) => {
    const trunk = branches.find((branch) => branch.path === key) ?? branches[0]
    return { key, trunk, forks: branches.filter((branch) => branch !== trunk) }
  })
}

/** What to call a group in the rail: the harness's name, or its folder if it will not load. */
export function groupLabel(group: HarnessGroup): string {
  return group.trunk.ok ? group.trunk.name : group.trunk.folder
}
