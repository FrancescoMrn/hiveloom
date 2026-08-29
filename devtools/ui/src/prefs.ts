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
  /**
   * The models the composer offers, whichever harness is open.
   *
   * A workbench setting, not a harness one: which models you are working with
   * today follows you across harnesses, while the model a harness *runs* is
   * part of what that harness is and travels with it to anyone else. A
   * narrowing and nothing else — empty means every model a key here can reach,
   * so a workbench nobody has configured has a full seat rather than an empty
   * one, and the open harness's own model is always offered on top.
   */
  seatModels: string[]
}

export const DEFAULT_PREFS: Prefs = {
  deliver: 'queue',
  trustOnCreate: true,
  theme: 'dark',
  evolveModel: '',
  seatModels: [],
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
      seatModels: seatModels(stored.seatModels),
    }
  } catch {
    return { ...DEFAULT_PREFS }
  }
}

/**
 * Storage is a text file a person can edit; read it as if they did.
 *
 * A stored object rather than a list is the shape this preference had while it
 * was per harness. Its values are folded together instead of dropped: the
 * models someone picked are the models they picked, and the id they were
 * picked under has simply stopped mattering.
 */
function seatModels(raw: unknown): string[] {
  const rows = Array.isArray(raw)
    ? raw
    : raw && typeof raw === 'object'
      ? Object.values(raw as Record<string, unknown>).flatMap((value) =>
          Array.isArray(value) ? value : [],
        )
      : []
  return [...new Set(rows.filter((item): item is string => typeof item === 'string' && !!item))]
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
