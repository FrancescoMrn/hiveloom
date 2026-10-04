/**
 * Workbench preferences: how *this* browser behaves, and nothing else.
 *
 * The line is deliberate. Anything that describes the harness — its model, its
 * tools, what a version is called — is written through hiveloom and lives on
 * disk beside the harness, because a second machine has to see it too. What is
 * left here is only how the window behaves for the person in front of it:
 * whether Enter queues or steers, which model the *evolver* is asked to draft
 * with, whether a harness you create is trusted immediately.
 *
 * Reads are defensive: a private window, cleared site data, or a browser that
 * refuses storage entirely all have to leave a working screen behind, so every
 * accessor falls back to the default rather than throwing.
 */

export type DeliverMode = 'queue' | 'steer'
export type Theme = 'dark' | 'light' | 'system'

export interface Prefs {
  /** What a message does when a run is already in flight. */
  deliver: DeliverMode
  /** New harnesses are yours by definition — but this stays a decision. */
  trustOnCreate: boolean
  theme: Theme
  /**
   * `provider/model-id` the evolver drafts proposals with, or '' for the
   * server's own choice. Not a spec field: `propose` takes it per call, which
   * is the right shape — it runs once per proposal, not once per turn.
   */
  evolveModel: string
}

export const DEFAULT_PREFS: Prefs = {
  deliver: 'queue',
  trustOnCreate: true,
  theme: 'dark',
  evolveModel: '',
}

const KEY = 'hiveloom.workbench.prefs'

export function loadPrefs(): Prefs {
  try {
    const raw = window.localStorage.getItem(KEY)
    if (!raw) return { ...DEFAULT_PREFS }
    const stored = JSON.parse(raw) as Partial<Prefs>
    return {
      deliver: stored.deliver === 'steer' ? 'steer' : 'queue',
      trustOnCreate: stored.trustOnCreate !== false,
      theme:
        stored.theme === 'light' || stored.theme === 'system' ? stored.theme : DEFAULT_PREFS.theme,
      evolveModel: typeof stored.evolveModel === 'string' ? stored.evolveModel : '',
    }
  } catch {
    return { ...DEFAULT_PREFS }
  }
}

export function savePrefs(prefs: Prefs): void {
  try {
    window.localStorage.setItem(KEY, JSON.stringify(prefs))
  } catch {
    // A browser that refuses storage still gets a working workbench; the
    // preference simply does not survive the tab.
  }
}

/**
 * Paint the chosen theme onto the document.
 *
 * `system` means "stamp nothing and let `prefers-color-scheme` decide", which
 * is why it clears the attribute rather than resolving the media query here.
 */
export function applyTheme(theme: Theme): void {
  const root = document.documentElement
  if (theme === 'system') root.removeAttribute('data-theme')
  else root.setAttribute('data-theme', theme)
}
