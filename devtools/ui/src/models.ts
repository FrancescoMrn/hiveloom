/**
 * Which models a picker should offer.
 *
 * Two filters, and they are answers to different questions.
 *
 * *Can this harness use it at all?* The provider registry is process-global,
 * and the API loads every harness's spec to fill the rail — so a provider
 * registered by one harness's extension (routing-lab's offline demo provider
 * is exactly this) ends up in the directory the API returns, visible to
 * harnesses that could never run it: the id is not registered when their spec
 * is built. The server marks those `available: false` for everyone but the
 * harness that declares the extension, and they are dropped first. This is not
 * a model-space question at all — it is a harness leaking into one.
 *
 * *Would a run on it start?* `api_key_set` is the server's word for "the key
 * is present in the API process", the same condition the first call would hit.
 * A picker of forty ids where three of them work is a list of ways to lose a
 * turn to an auth error.
 *
 * The one escape: the selector already in use is always offered. A `<select>`
 * whose value names no option silently renders as its first one, so filtering
 * out the model a run is on would show the wrong model — the failure worse
 * than showing an unusable one. Nothing else is added back. There used to be a
 * second escape, "if no provider has a key, show them all", from when the API
 * only read its own environment and a key in the harness's `.env` was
 * invisible to it. The server reads both places now — the same two a run
 * reads — so a provider reported keyless is keyless, and answering that with
 * the whole catalogue would be guessing over an answer we have.
 */
import type { Provider } from './types'

/** Providers this harness may pick at all — see `scope` in types.ts. */
export function usableProviders(providers: Provider[] | null): Provider[] {
  return (providers ?? []).filter((provider) => provider.available !== false)
}

/** Providers a run can be started on now — nothing else, however short. */
export function activeProviders(providers: Provider[] | null, keep = ''): Provider[] {
  const rows = usableProviders(providers).filter(
    (provider) => provider.api_key_set && provider.models.length > 0,
  )

  const held = keep.split('/')[0]
  if (held && !rows.some((provider) => provider.name === held)) {
    // Searched across everything, not just what is usable: the model a run is
    // already on has to stay visible even once it stops being offerable.
    const kept = (providers ?? []).find((provider) => provider.name === held)
    if (kept) return [...rows, kept]
  }
  return rows
}

/**
 * The `provider/model-id` a harness runs on when nothing overrides it.
 *
 * Read off the spec the API already sends with the harness, so the picker can
 * name the default model instead of calling it "the harness's own model" —
 * a row in a list of models that was not itself a model, and told you nothing
 * about which one you were about to run.
 */
export function specSelector(harness: { spec?: Record<string, unknown> } | null): string {
  const model = (harness?.spec as { model?: { provider?: string; id?: string } } | undefined)?.model
  return model?.provider && model?.id ? `${model.provider}/${model.id}` : ''
}

export interface ModelOption {
  /** A real `provider/model-id`, always — the picker holds nothing else. */
  value: string
  label: string
  /** True for the one the spec names, which is where the picker starts. */
  spec: boolean
}

/**
 * Every row of a model picker: usable models, and nothing that is not a model.
 *
 * A harness is not bound to a model. The spec names the one a run starts on,
 * and any run can be moved onto another — so this list is simply the models
 * that can really be used, with the spec's among them rather than standing
 * outside the list as an unnamed "the harness's own model" row.
 *
 * `current` (the workspace's override, '' when there is none) is offered too
 * even if it would otherwise be filtered out: a `<select>` whose value matches
 * no option renders as its first one, and showing the wrong model is worse
 * than showing an unusable one.
 *
 * `shortlist` narrows the usable models to the ones chosen for this harness in
 * Settings. Empty means no narrowing — it is a filter someone opted into, not
 * a list that starts empty and has to be filled before the seat works.
 */
export function modelOptions(
  providers: Provider[] | null,
  own: string,
  current = '',
  shortlist: string[] = [],
): ModelOption[] {
  const selectors = narrow(modelSelectors(providers, current), shortlist, [own, current])
  // Both anchors are always present, not just whichever is in force: with an
  // override set *and* the spec's provider unusable here, leaving the spec's
  // model out would make the model a run starts on the one model the seat
  // could not go back to.
  if (own && !selectors.includes(own)) selectors.unshift(own)
  const rows = selectors.map((selector) => ({
    value: selector,
    label: selector,
    spec: selector === own,
  }))
  // Only when the spec's model cannot be read at all — the harness detail
  // arrived without a spec. There is still a model behind the run; it just has
  // no name to show, and a picker that silently displayed a *different* model
  // as selected would be worse than one honest row.
  if (!own) rows.unshift({ value: '', label: 'the model in the spec', spec: true })
  return rows
}

/**
 * Apply a shortlist, keeping the rows that have to be there whatever it says.
 *
 * The spec's model and the override in force are anchors: one is where a run
 * starts and the other is where this workspace already is, so a shortlist that
 * excluded either would hide the model on screen rather than narrow a list.
 */
function narrow(selectors: string[], shortlist: string[], anchors: string[]): string[] {
  if (shortlist.length === 0) return selectors
  const keep = new Set([...shortlist, ...anchors.filter(Boolean)])
  return selectors.filter((selector) => keep.has(selector))
}

/**
 * What the workspace should store when a picker row is chosen.
 *
 * Picking the model the spec already names is not an override, so it clears
 * one rather than pinning the workspace to the same model by another route —
 * and the harness then keeps following its spec if that spec is edited.
 */
export function overrideFor(selector: string, own: string): string {
  return selector === own ? '' : selector
}

/**
 * Every model the *workbench* can run something on.
 *
 * Global providers only. One registered by a harness's extension exists inside
 * that harness — offering it as a workbench-wide model would be offering
 * something most harnesses cannot load.
 */
export function workbenchModels(providers: Provider[] | null): string[] {
  return modelSelectors((providers ?? []).filter((provider) => provider.scope !== 'harness'))
}

/** `provider/model-id` selectors for a picker, in directory order. */
export function modelSelectors(providers: Provider[] | null, keep = ''): string[] {
  const rows = activeProviders(providers, keep).flatMap((provider) =>
    provider.models.map((model) => `${provider.name}/${model.id}`),
  )
  // An open-catalog provider takes ids beyond the ones it lists, so the model
  // in use may be a real id the directory has never heard of.
  if (keep && !rows.includes(keep)) rows.push(keep)
  return rows
}
