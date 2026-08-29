/**
 * Settings: three panes, split by who owns the answer.
 *
 * **General** is this browser's — how Enter behaves, whether a harness you
 * create is trusted on the spot, which way the window looks. None of it
 * touches a harness, so none of it is written to one.
 *
 * **Models** is the harness's, and every field here is written through
 * hiveloom's validated construction API rather than by editing YAML in the
 * browser: `model.provider` and `model.id` validate against each other and so
 * move in one commit, and each numeric field is its own commit that the schema
 * can refuse. The evolution model is the exception that proves the rule — it
 * is not a spec field at all, because the evolver takes it per call, once per
 * proposal rather than once per turn.
 *
 * **Providers** is the environment's: which keys this API process can see. It
 * reports whether a key is *present*, never its value, which is the same
 * discipline `hiveloom models` keeps.
 *
 * Everything else about a harness stays in the Spec tab, where the YAML is the
 * truth and a bad edit is rolled back rather than half-applied.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import { modelSelectors, usableProviders, workbenchModels } from '../models'
import type { DeliverMode, Prefs, Theme } from '../prefs'
import type { HarnessDetail, MemoryRecord, Provider } from '../types'
import { Notice } from './common'

type Pane = 'general' | 'workbench' | 'memory' | 'models' | 'providers'

/**
 * Two of these are about the machine you are working on and two are about the
 * harness, and the line between them is the point. Which models this workbench
 * offers is a development setting — it follows you from harness to harness.
 * Which model a harness *runs* is part of what that harness is, and travels
 * with it to anyone else who checks it out.
 */
const PANES: { id: Pane; label: string; icon: string }[] = [
  { id: 'general', label: 'General', icon: 'ph-gear-six' },
  { id: 'workbench', label: 'Workbench', icon: 'ph-toolbox' },
  { id: 'memory', label: 'Memory', icon: 'ph-brain' },
  { id: 'models', label: 'Models', icon: 'ph-brain' },
  { id: 'providers', label: 'Providers', icon: 'ph-plugs' },
]

/** The same three places, in the width a card has for them. */
const SOURCE_TAG: Record<string, string> = {
  process: 'environment',
  workbench: 'workbench',
  harness: 'harness .env',
}

/** Said the same way wherever a key's origin is shown. */
const KEY_SOURCE: Record<string, string> = {
  process: 'set in the environment the API started in',
  workbench: "from this workbench's ~/.hiveloom/.env",
  harness: "from this harness's own .env",
  none: 'no key needed',
}

const THEMES: { id: Theme; label: string; icon: string }[] = [
  { id: 'light', label: 'Light', icon: 'ph-sun' },
  { id: 'dark', label: 'Dark', icon: 'ph-moon' },
  { id: 'system', label: 'System', icon: 'ph-desktop' },
]

const DELIVERY: { id: DeliverMode; label: string }[] = [
  { id: 'queue', label: 'Queue' },
  { id: 'steer', label: 'Steer' },
]

export function Settings({
  harness,
  providers,
  prefs,
  onPrefs,
  onClose,
  onSaved,
  onOpenSpec,
}: {
  harness: HarnessDetail | null
  providers: Provider[] | null
  prefs: Prefs
  onPrefs: (next: Prefs) => void
  onClose: () => void
  onSaved: () => Promise<void>
  onOpenSpec: () => void
}) {
  const [pane, setPane] = useState<Pane>(harness ? 'models' : 'workbench')
  const panes = harness ? PANES : PANES.filter((item) => item.id !== 'models')

  return (
    <div className="scrim" onClick={onClose}>
      <div className="settings-modal rise" onClick={(event) => event.stopPropagation()}>
        <header>
          <h2 style={{ fontSize: 16 }}>Settings</h2>
          <span className="settings-subject ellipsis">{harness?.name ?? 'Workbench'}</span>
          {harness && (
            <button
              className="settings-spec"
              title={harness.yaml_path}
              onClick={() => {
                onClose()
                onOpenSpec()
              }}
            >
              <i className="ph ph-file-code" />
              Open configuration file
            </button>
          )}
          <button className="settings-close" onClick={onClose} title="Close">
            <i className="ph ph-x" />
          </button>
        </header>

        <div className="settings-body">
          <nav className="settings-nav">
            {panes.map((item) => (
              <button
                key={item.id}
                data-on={pane === item.id ? '1' : '0'}
                onClick={() => setPane(item.id)}
              >
                <i className={`ph ${item.icon}`} />
                {item.label}
              </button>
            ))}
          </nav>

          <div className="settings-pane">
            {pane === 'general' && <General prefs={prefs} onPrefs={onPrefs} />}
            {pane === 'workbench' && (
              <Workbench providers={providers} prefs={prefs} onPrefs={onPrefs} />
            )}
            {pane === 'memory' && <Memory harness={harness} />}
            {pane === 'models' && harness && (
              <Models
                harness={harness}
                providers={providers}
                prefs={prefs}
                onPrefs={onPrefs}
                onSaved={onSaved}
              />
            )}
            {pane === 'providers' && (
              <Providers
                providers={providers}
                harness={harness}
                onUse={async (selector) => {
                  if (!harness) return
                  await api.setModel(harness.id, { selector })
                  await onSaved()
                  setPane('models')
                }}
              />
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

function Memory({ harness }: { harness: HarnessDetail | null }) {
  const [rows, setRows] = useState<MemoryRecord[] | null>(null)
  const [content, setContent] = useState('')
  const [scope, setScope] = useState<'global' | 'harness'>(harness ? 'harness' : 'global')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      setRows(await api.memories(harness?.id))
      setError(null)
    } catch (exc) {
      setRows([])
      setError(String(exc))
    }
  }, [harness?.id])

  useEffect(() => { void load() }, [load])

  const save = async () => {
    const clean = content.trim()
    if (!clean) return
    setBusy(true)
    try {
      await api.remember(clean, scope === 'harness' ? harness?.id : undefined)
      setContent('')
      await load()
    } catch (exc) {
      setError(String(exc))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div>
      <p className="settings-lede">
        Durable facts the copilot may recall in another conversation. Memory is explicit,
        inspectable, and deletable; ordinary messages remain in their conversation only.
      </p>
      <section className="settings-card memory-compose">
        <div className="settings-name">Remember a preference or convention</div>
        <textarea
          className="v-input"
          rows={3}
          value={content}
          placeholder="For example: Prefer concise verifier feedback with the failing field named."
          onChange={(event) => setContent(event.target.value)}
        />
        <div className="memory-actions">
          <select
            className="v-input"
            value={scope}
            onChange={(event) => setScope(event.target.value as 'global' | 'harness')}
          >
            <option value="global">All conversations</option>
            {harness && <option value="harness">Only {harness.name}</option>}
          </select>
          <button className="v-btn v-btn-primary" disabled={busy || !content.trim()} onClick={() => void save()}>
            <i className="ph ph-plus" /> Remember
          </button>
        </div>
      </section>
      {error && <Notice icon="ph-warning" tone="err" title="Memory unavailable" body={error} />}
      <div className="memory-list">
        {rows === null ? (
          <div className="empty compact">Loading memories…</div>
        ) : rows.length === 0 ? (
          <div className="empty compact">Nothing has been remembered yet.</div>
        ) : rows.map((memory) => (
          <div className="memory-row" key={memory.id}>
            <div>
              <span className="rail-tag">{memory.scope === 'global' ? 'all conversations' : harness?.name ?? memory.harness_id}</span>
              <p>{memory.content}</p>
            </div>
            <button
              className="icon-btn"
              title="Forget this memory"
              onClick={() => void api.forget(memory.id).then(load).catch((exc) => setError(String(exc)))}
            >
              <i className="ph ph-trash" />
            </button>
          </div>
        ))}
      </div>
    </div>
  )
}

/**
 * The workbench pane: what *this machine* can run, not what a harness is.
 *
 * The models offered under the composer are a property of the workbench —
 * whichever harness is open, these are the ones you are working with today.
 * The harness's own run model is always offered on top of this, because it is
 * what a run starts on and a seat that could not show it would be lying.
 *
 * Only globally registered providers are listed. A provider that a harness's
 * extension registers exists inside that one harness, so it is not something
 * the workbench can offer everywhere.
 */
function Workbench({
  providers,
  prefs,
  onPrefs,
}: {
  providers: Provider[] | null
  prefs: Prefs
  onPrefs: (next: Prefs) => void
}) {
  const reachable = useMemo(() => workbenchModels(providers), [providers])
  const chosen = prefs.seatModels

  const setSeat = (models: string[]) => onPrefs({ ...prefs, seatModels: models })

  const toggle = (selector: string) => {
    // The first click on a full list means "just this one", not "everything
    // except this one" — starting from the implicit all-reachable list would
    // store a shortlist the size of the directory.
    const from = chosen.length === 0 ? reachable : chosen
    const next = from.includes(selector)
      ? from.filter((item) => item !== selector)
      : [...from, selector]
    // All of them is the default again, not a list that happens to name them
    // all — so a model that appears later is offered without being re-picked.
    setSeat(next.length === reachable.length ? [] : next)
  }

  return (
    <div>
      <p className="settings-lede">
        How this workbench behaves while you develop. Nothing here is written to a harness — a
        harness carries its own model, and these settings follow you across all of them.
      </p>

      <section className="settings-card">
        <div className="settings-card-head">
          <i className="ph ph-list-checks" style={{ color: 'var(--acc)' }} />
          <div className="settings-name">Models offered under the composer</div>
          <span className="settings-path">
            {chosen.length === 0 ? 'all reachable' : `${chosen.length} of ${reachable.length}`}
          </span>
        </div>
        <p className="settings-help">
          A harness is not bound to a model: any run can be moved onto another one, and comparing
          two models on one harness is the loop this tool exists for. These are the ones you want to
          hand for that — they appear as buttons beside the composer. Nothing chosen means every
          model a key here can reach. The open harness's own run model is always offered too.
        </p>
        {reachable.length === 0 ? (
          <p className="settings-help" style={{ marginTop: 10 }}>
            No provider has a key on this machine yet, so there is nothing to choose between — see
            the Providers pane.
          </p>
        ) : (
          <>
            <div className="seat-picks">
              {reachable.map((selector) => {
                const on = chosen.length === 0 || chosen.includes(selector)
                return (
                  <label
                    key={selector}
                    className="seat-pick"
                    data-on={on ? '1' : '0'}
                    title={selector}
                  >
                    <input type="checkbox" checked={on} onChange={() => toggle(selector)} />
                    <span className="mono ellipsis">{selector}</span>
                  </label>
                )
              })}
            </div>
            {chosen.length > 0 && (
              <button
                className="v-btn v-btn-ghost v-btn-sm"
                style={{ marginTop: 12 }}
                onClick={() => setSeat([])}
              >
                <i className="ph ph-arrow-counter-clockwise" />
                Offer every reachable model
              </button>
            )}
          </>
        )}
      </section>
    </div>
  )
}

function General({ prefs, onPrefs }: { prefs: Prefs; onPrefs: (next: Prefs) => void }) {
  return (
    <div>
      <div className="settings-row">
        <div>
          <div className="settings-name">Enter behavior while busy</div>
          <p className="settings-help">
            What a message does when a run is already in flight. ⌘/Ctrl + Enter always uses the
            other behavior.
          </p>
        </div>
        <div className="mode-toggle">
          {DELIVERY.map((mode) => (
            <button
              key={mode.id}
              data-on={prefs.deliver === mode.id ? '1' : '0'}
              onClick={() => onPrefs({ ...prefs, deliver: mode.id })}
            >
              {mode.label}
            </button>
          ))}
        </div>
      </div>

      <div className="settings-row">
        <div>
          <div className="settings-name">Trust new harnesses on create</div>
          <p className="settings-help">
            Code hooks run with your privileges, so this stays off for anything you did not write.
            It only ever applies to a harness created here, from a directory you named.
          </p>
        </div>
        <div className="mode-toggle">
          {[true, false].map((value) => (
            <button
              key={String(value)}
              data-on={prefs.trustOnCreate === value ? '1' : '0'}
              onClick={() => onPrefs({ ...prefs, trustOnCreate: value })}
            >
              {value ? 'Trusted' : 'Ask first'}
            </button>
          ))}
        </div>
      </div>

      <div className="settings-row block">
        <div className="settings-name">Appearance</div>
        <div className="theme-grid">
          {THEMES.map((theme) => (
            <button
              key={theme.id}
              className="theme-card"
              data-on={prefs.theme === theme.id ? '1' : '0'}
              onClick={() => onPrefs({ ...prefs, theme: theme.id })}
            >
              <i className={`ph ${theme.icon}`} />
              {theme.label}
            </button>
          ))}
        </div>
      </div>
    </div>
  )
}

function Models({
  harness,
  providers,
  prefs,
  onPrefs,
  onSaved,
}: {
  harness: HarnessDetail
  providers: Provider[] | null
  prefs: Prefs
  onPrefs: (next: Prefs) => void
  onSaved: () => Promise<void>
}) {
  const spec = harness.spec as
    | { model?: { provider?: string; id?: string; temperature?: number | null }; context?: { max_input_tokens?: number } }
    | undefined

  const [provider, setProvider] = useState(spec?.model?.provider ?? '')
  const [modelId, setModelId] = useState(spec?.model?.id ?? '')
  const [temperature, setTemperature] = useState(
    spec?.model?.temperature === null || spec?.model?.temperature === undefined
      ? ''
      : String(spec.model.temperature),
  )
  const [maxInput, setMaxInput] = useState(String(spec?.context?.max_input_tokens ?? ''))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState<string | null>(null)

  const chosen = useMemo(
    () => providers?.find((item) => item.name === provider) ?? null,
    [providers, provider],
  )
  const meta = useMemo(
    () => chosen?.models.find((item) => item.id === modelId) ?? null,
    [chosen, modelId],
  )

  // The evolver runs here and now, so it is offered only models that can run —
  // unlike the run-model card above, which is where a missing key gets fixed
  // and therefore has to show the providers that are missing one.
  const allModels = useMemo(
    () => modelSelectors(providers, prefs.evolveModel),
    [providers, prefs.evolveModel],
  )

  /**
   * Every provider this harness could be moved onto — including the ones whose
   * key is missing, because this is the page where that gets fixed and the
   * `key is not set here` line under the picker is the fix's own signpost.
   * What it does drop is a provider another harness's extension registered:
   * the id would validate in this process, where every spec has been loaded,
   * and then fail to resolve anywhere else.
   */
  const offered = useMemo(() => usableProviders(providers), [providers])

  const save = async () => {
    setBusy(true)
    setError(null)
    try {
      const result = await api.setModel(harness.id, {
        selector: `${provider}/${modelId.trim()}`,
        temperature: temperature.trim() === '' ? '' : Number(temperature),
        max_input_tokens: Number(maxInput),
      })
      await onSaved()
      setSaved(
        `Runs from now on use ${result.provider}/${result.id}, with a ${result.max_input_tokens.toLocaleString()} token context budget` +
          `${result.temperature === null ? ' and no temperature set' : ` at temperature ${result.temperature}`}. ` +
          'Runs already recorded keep the model path they ran with.',
      )
    } catch (exc) {
      setError(String(exc))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div>
      <p className="settings-lede">
        Model changes are written through hiveloom's validated construction API, exactly as{' '}
        <code>hiveloom set model</code> would. Runs already recorded keep the model path they ran
        with.
      </p>

      <section className="settings-card">
        <div className="settings-card-head">
          <i className="ph ph-brain" style={{ color: 'var(--evo)' }} />
          <div className="settings-name">Run model</div>
          <span className="mono settings-path">
            {provider}/{modelId}
          </span>
        </div>
        <p className="settings-help">
          Every turn of every run on this harness, unless that run switches models.
        </p>
        <div className="settings-pair">
          <select
            className="v-input"
            value={provider}
            onChange={(event) => {
              setProvider(event.target.value)
              setSaved(null)
              const next = providers?.find((item) => item.name === event.target.value)
              setModelId(next?.models[0]?.id ?? '')
            }}
          >
            {providers === null && <option value="">loading…</option>}
            {offered.map((item) => (
              <option key={item.name} value={item.name}>
                {item.label || item.name}
              </option>
            ))}
          </select>
          {chosen?.open_catalog ? (
            <>
              <input
                className="v-input mono"
                value={modelId}
                placeholder="model id"
                list="known-models"
                onChange={(event) => {
                  setModelId(event.target.value)
                  setSaved(null)
                }}
              />
              <datalist id="known-models">
                {chosen.models.map((model) => (
                  <option key={model.id} value={model.id} />
                ))}
              </datalist>
            </>
          ) : (
            <select
              className="v-input mono"
              value={modelId}
              onChange={(event) => {
                setModelId(event.target.value)
                setSaved(null)
              }}
            >
              {chosen?.models.map((model) => (
                <option key={model.id} value={model.id}>
                  {model.id}
                </option>
              ))}
            </select>
          )}
        </div>

        {chosen && (
          <div className="key-state" data-ok={chosen.api_key_set ? '1' : '0'}>
            <i className={`ph ${chosen.api_key_set ? 'ph-check-circle' : 'ph-warning'}`} />
            {chosen.api_key_env
              ? chosen.api_key_set
                ? `${chosen.api_key_env} · ${KEY_SOURCE[chosen.api_key_from] ?? 'set'}`
                : `${chosen.api_key_env} is not set — see the Providers pane`
              : 'No API key needed'}
          </div>
        )}

        {chosen?.open_catalog && (
          <p className="settings-help" style={{ marginTop: 8 }}>
            This provider takes ids beyond the listed ones — new releases, aggregator routes,
            whatever a local server is serving. An unlisted id is costed at hiveloom's conservative
            fallback price, so a budget guardrail never thinks a run is cheaper than it is.
          </p>
        )}

        <div className="model-meta">
          <MetaCell
            label="context"
            value={meta?.context_window ? compact(meta.context_window) : '—'}
          />
          <MetaCell label="$ / M in" value={price(meta?.input_cost_per_mtok)} />
          <MetaCell label="$ / M out" value={price(meta?.output_cost_per_mtok)} />
          <MetaCell label="provider" value={chosen?.open_catalog ? 'open catalog' : 'listed ids'} />
        </div>
      </section>

      <section className="settings-card">
        <div className="settings-card-head">
          <i className="ph ph-sparkle" style={{ color: 'var(--acc)' }} />
          <div className="settings-name">Evolution model</div>
        </div>
        <p className="settings-help">
          Drafts proposals from recorded failures. Usually the strongest model you have — it runs
          once per proposal, not once per turn. Kept in this browser rather than in the spec,
          because it is not part of what the harness <em>is</em>.
        </p>
        <select
          className="v-input mono"
          style={{ marginTop: 12 }}
          value={prefs.evolveModel}
          onChange={(event) => onPrefs({ ...prefs, evolveModel: event.target.value })}
        >
          <option value="">hiveloom's own strong-model default</option>
          {allModels.map((selector) => (
            <option key={selector} value={selector}>
              {selector}
            </option>
          ))}
        </select>
      </section>

      <section className="settings-grid">
        <div className="settings-card">
          <div className="settings-name">Max input tokens per call</div>
          <p className="settings-help">Context is trimmed to fit before the call is made.</p>
          <input
            className="v-input mono"
            value={maxInput}
            inputMode="numeric"
            onChange={(event) => {
              setMaxInput(event.target.value)
              setSaved(null)
            }}
          />
        </div>
        <div className="settings-card">
          <div className="settings-name">Temperature</div>
          <p className="settings-help">
            Left at 0 for anything a validator has to judge. Empty omits the field, which some
            models require.
          </p>
          <input
            className="v-input mono"
            value={temperature}
            placeholder="omitted"
            inputMode="decimal"
            onChange={(event) => {
              setTemperature(event.target.value)
              setSaved(null)
            }}
          />
        </div>
      </section>

      {error && (
        <div style={{ marginTop: 14 }}>
          <Notice
            icon="ph-warning-octagon"
            tone="err"
            title="Refused — the spec on disk is unchanged"
            body={error}
          />
        </div>
      )}
      {saved && (
        <div style={{ marginTop: 14 }}>
          <Notice icon="ph-check-circle" tone="ok" title="Saved and revalidated" body={saved} />
        </div>
      )}

      <div className="settings-actions">
        <span className="settings-help">
          Tools, validators, guardrails and loop policy live in the spec.
        </span>
        <button
          className="v-btn v-btn-primary"
          onClick={() => void save()}
          disabled={busy || !provider || !modelId.trim()}
        >
          {busy ? (
            <i className="ph ph-circle-notch" style={{ animation: 'spin 1s linear infinite' }} />
          ) : (
            <i className="ph ph-check" />
          )}
          Save model settings
        </button>
      </div>
    </div>
  )
}

function Providers({
  providers,
  harness,
  onUse,
}: {
  providers: Provider[] | null
  harness: HarnessDetail | null
  onUse: (selector: string) => Promise<void>
}) {
  const spec = harness?.spec as { model?: { provider?: string; id?: string } } | undefined
  const active = spec?.model?.provider

  if (providers === null) return <div className="empty compact">Loading the model directory…</div>

  return (
    <div>
      <p className="settings-lede">
        A key can live in three places, and a run takes the first that has one: the environment the
        API was started in, this workbench's <code>~/.hiveloom/.env</code> — the keys this machine
        develops with, which every harness in the rail can use — or a harness's own{' '}
        <code>.env</code>, which travels with that harness. Adding one to the workbench file needs
        a page reload, not a restart. Keys are never written into a spec, and never shown here —
        only whether one is present, and which of the three it came from.
      </p>
      <div className="provider-cards">
        {providers.map((provider) => (
          <div key={provider.name} className="provider-card" data-on={active === provider.name ? '1' : '0'}>
            <div className="provider-head">
              <div className="settings-name">{provider.label || provider.name}</div>
              {active === provider.name && <span className="rail-tag">in use</span>}
              <span className="key-state mono" data-ok={provider.api_key_set ? '1' : '0'}>
                <i className={`ph ${provider.api_key_set ? 'ph-check-circle' : 'ph-warning'}`} />
                {!provider.api_key_env
                  ? 'no key needed'
                  : provider.api_key_set
                    ? `${provider.api_key_env} · ${SOURCE_TAG[provider.api_key_from] ?? 'set'}`
                    : `${provider.api_key_env} missing`}
              </span>
              {harness && (
                <button
                  className="v-btn v-btn-ghost v-btn-sm"
                  style={{ marginLeft: 'auto' }}
                  disabled={provider.models.length === 0}
                  onClick={() => void onUse(`${provider.name}/${provider.models[0]?.id ?? ''}`)}
                >
                  Use
                </button>
              )}
            </div>
            <div className="provider-models">
              {provider.models.map((model) => (
                <span
                  key={model.id}
                  className="mono model-chip"
                  data-on={
                    active === provider.name && spec?.model?.id === model.id ? '1' : '0'
                  }
                >
                  {model.id}
                </span>
              ))}
              {provider.models.length === 0 && (
                <span className="settings-help">
                  No ids registered — this provider routes whatever you name.
                </span>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

function MetaCell({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="mono model-meta-value">{value}</div>
      <div className="v-label" style={{ marginTop: 2 }}>
        {label}
      </div>
    </div>
  )
}

function price(value: number | undefined): string {
  if (value === undefined) return '—'
  if (value === 0) return 'free'
  return `$${value.toFixed(2)}`
}

function compact(tokens: number): string {
  if (tokens >= 1_000_000) return `${(tokens / 1_000_000).toFixed(1)}M`
  if (tokens >= 1000) return `${Math.round(tokens / 1000)}k`
  return String(tokens)
}

/**
 * The model directory, fetched once for the shell and handed to every pane
 * that needs it rather than each fetching its own copy.
 *
 * Keyed by harness, because the answer depends on which one is open: a
 * provider registered by a harness extension is only usable inside the harness
 * that declares it, and the server needs to be told which that is. Re-fetching
 * on a harness switch is one small request, and the alternative is offering a
 * model whose provider is not registered when that harness's spec is built.
 */
export function useProviders(harnessId: string | null): Provider[] | null {
  const [providers, setProviders] = useState<Provider[] | null>(null)
  useEffect(() => {
    let live = true
    api
      .providers(harnessId ?? undefined)
      .then((rows) => live && setProviders(rows))
      .catch(() => live && setProviders([]))
    return () => {
      live = false
    }
  }, [harnessId])
  return providers
}
