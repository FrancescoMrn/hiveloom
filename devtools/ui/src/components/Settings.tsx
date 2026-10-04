/**
 * Workbench settings: how this machine develops, never what a harness is.
 *
 * A harness carries its own model in its spec, and that travels with it. What
 * lives here is the set of providers this machine has keys for, the models
 * they put in the composer, and the default model a run falls back to when a
 * harness names a model whose provider is not set up here. A harness's own
 * run model is edited in its workspace (the Settings tab beside Overview).
 */
import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import type { DeliverMode, Prefs, Theme } from '../prefs'
import type { MemoryRecord, ProviderOffer, WorkbenchDirectory, WorkbenchProvider } from '../types'
import { providerSlug, publishDirectory, useWorkbenchDirectory } from '../workbench'
import { Notice } from './common'
import { ModelPicker } from './ModelPicker'

type Pane = 'general' | 'models' | 'memory'

const PANES: { id: Pane; label: string; icon: string }[] = [
  { id: 'general', label: 'General', icon: 'ph-gear-six' },
  { id: 'models', label: 'Models', icon: 'ph-cube' },
  { id: 'memory', label: 'Memory', icon: 'ph-brain' },
]

const OTHER = '__other'

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
  prefs,
  onPrefs,
  onClose,
  strongModel = '',
  initialPane = 'models',
}: {
  prefs: Prefs
  onPrefs: (next: Prefs) => void
  onClose: () => void
  /** What an unset evolution model resolves to, named in its picker. */
  strongModel?: string
  initialPane?: Pane
}) {
  const [pane, setPane] = useState<Pane>(initialPane)

  return (
    <div className="scrim" onClick={onClose}>
      <div className="settings-modal rise" onClick={(event) => event.stopPropagation()}>
        <header>
          <h2 style={{ fontSize: 16 }}>Settings</h2>
          <button className="settings-close" onClick={onClose} title="Close">
            <i className="ph ph-x" />
          </button>
        </header>

        <div className="settings-body">
          <nav className="settings-nav">
            {PANES.map((item) => (
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
            {pane === 'models' && <Models prefs={prefs} onPrefs={onPrefs} strongModel={strongModel} />}
            {pane === 'memory' && <Memory harness={null} />}
          </div>
        </div>
      </div>
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
  prefs,
  onPrefs,
  strongModel,
}: {
  prefs: Prefs
  onPrefs: (next: Prefs) => void
  strongModel: string
}) {
  const directory = useWorkbenchDirectory()
  const [editing, setEditing] = useState<WorkbenchProvider | 'new' | null>(null)
  const [error, setError] = useState<string | null>(null)

  const remove = async (provider: WorkbenchProvider) => {
    if (!window.confirm(`Remove ${provider.label}? Its key is deleted from this machine.`)) return
    try {
      publishDirectory(await api.deleteProvider(provider.name))
      setError(null)
    } catch (exc) {
      setError(String(exc))
    }
  }

  const setDefault = async (selector: string) => {
    try {
      publishDirectory(await api.setDefaultModel(selector))
      setError(null)
    } catch (exc) {
      setError(String(exc))
    }
  }

  const strongId = strongModel.split('/').slice(1).join('/')

  return (
    <div>
      <div className="settings-pane-title">Models</div>
      <p className="settings-lede">Enter your API keys to use models from the following providers.</p>

      {directory === null ? (
        <div className="empty compact">Loading providers…</div>
      ) : (
        <div className="wb-providers">
          {directory.providers.map((provider) => (
            <div className="wb-provider" key={provider.name}>
              <div className="wb-provider-name">
                {provider.label}
                <span
                  className="wb-dot"
                  data-ok={provider.key_set ? '1' : '0'}
                  title={provider.key_set ? 'Key set' : `${provider.api_key_env} is not set`}
                />
              </div>
              <span className="wb-provider-meta">
                {provider.models.length
                  ? `${provider.models.length} model${provider.models.length === 1 ? '' : 's'}`
                  : 'no models yet'}
                {provider.key_from === 'process' && ' · key from the environment'}
              </span>
              <button className="v-btn v-btn-ghost v-btn-sm" onClick={() => setEditing(provider)}>
                Edit
              </button>
              {provider.removable && (
                <button className="wb-delete" onClick={() => void remove(provider)}>
                  Delete
                </button>
              )}
            </div>
          ))}
          <button className="wb-add" onClick={() => setEditing('new')}>
            <i className="ph ph-plus" /> Add model provider
          </button>
        </div>
      )}

      {error && (
        <div style={{ marginTop: 12 }}>
          <Notice icon="ph-warning-octagon" tone="err" title="Not saved" body={error} />
        </div>
      )}

      <section className="settings-card" style={{ marginTop: 22 }}>
        <div className="settings-card-head">
          <i className="ph ph-arrow-bend-down-right" style={{ color: 'var(--acc)' }} />
          <div className="settings-name">Default model</div>
        </div>
        <p className="settings-help">
          What a harness runs on when the model its spec names belongs to a provider that is not set
          up here — for that run only. The spec is never changed, and once you add that provider the
          harness is back on its own model.
        </p>
        <div style={{ marginTop: 12 }}>
          <ModelPicker
            directory={directory}
            value={directory?.default_model ?? ''}
            placeholder={
              directory?.effective_default
                ? `First available — ${directory.effective_default}`
                : 'Add a provider first'
            }
            ariaLabel="Default model"
            onChange={(selector) => void setDefault(selector)}
          />
        </div>
      </section>

      <section className="settings-card">
        <div className="settings-card-head">
          <i className="ph ph-sparkle" style={{ color: 'var(--acc)' }} />
          <div className="settings-name">Evolution model</div>
        </div>
        <p className="settings-help">
          Drafts improvement proposals from recorded failures. Usually the strongest model you have —
          it runs once per proposal, not once per turn.
        </p>
        <div style={{ marginTop: 12 }}>
          <ModelPicker
            directory={directory}
            value={prefs.evolveModel}
            defaultLabel={strongId ? `Default · ${strongId}` : 'Default'}
            ariaLabel="Evolution model"
            onChange={(selector) => onPrefs({ ...prefs, evolveModel: selector })}
          />
        </div>
      </section>

      {editing && directory && (
        <ProviderDialog
          directory={directory}
          provider={editing === 'new' ? null : editing}
          onClose={() => setEditing(null)}
        />
      )}
    </div>
  )
}

/** Add a provider, or edit one's key and models. */
function ProviderDialog({
  directory,
  provider,
  onClose,
}: {
  directory: WorkbenchDirectory
  provider: WorkbenchProvider | null
  onClose: () => void
}) {
  const added = new Set(directory.providers.map((row) => row.name))
  const offers = directory.catalog.filter((offer) => !added.has(offer.name))
  const [kind, setKind] = useState<string>(
    provider ? (provider.custom ? OTHER : provider.name) : (offers[0]?.name ?? OTHER),
  )
  const offer: ProviderOffer | undefined = directory.catalog.find((item) => item.name === kind)
  const custom = kind === OTHER
  const catalogModels = offer?.models === 'catalog'

  const [label, setLabel] = useState(provider?.custom ? provider.label : '')
  const [baseUrl, setBaseUrl] = useState(provider?.custom ? provider.base_url : '')
  const [apiKey, setApiKey] = useState('')
  const [picked, setPicked] = useState<string[]>(
    provider ? provider.models.map((model) => model.id) : [],
  )
  const [draftId, setDraftId] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // A new catalog provider starts with all of its current models offered.
  useEffect(() => {
    if (provider) return
    setPicked(offer?.models === 'catalog' ? offer.choices.map((model) => model.id) : [])
  }, [kind, offer, provider])

  const name = provider ? provider.name : custom ? providerSlug(label) : kind
  const keyStored = !!provider?.key_set
  const canSave =
    !!name && (keyStored || !!apiKey.trim()) && (!custom || (!!label.trim() && !!baseUrl.trim()))

  const addId = () => {
    const id = draftId.trim()
    if (id && !picked.includes(id)) setPicked([...picked, id])
    setDraftId('')
  }

  const save = async () => {
    setBusy(true)
    setError(null)
    try {
      const pending = draftId.trim() && !picked.includes(draftId.trim()) ? [...picked, draftId.trim()] : picked
      const body: Parameters<typeof api.putProvider>[1] = { models: pending }
      if (apiKey.trim()) body.api_key = apiKey.trim()
      if (custom) Object.assign(body, { label: label.trim(), base_url: baseUrl.trim() })
      publishDirectory(await api.putProvider(name, body))
      onClose()
    } catch (exc) {
      setError(String(exc))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="scrim wb-dialog-scrim" onClick={onClose}>
      <div className="wb-dialog rise" onClick={(event) => event.stopPropagation()} role="dialog" aria-label="Model provider">
        <header>
          <h3>{provider ? `Edit ${provider.label}` : 'Add model provider'}</h3>
          <button className="settings-close" onClick={onClose} title="Close">
            <i className="ph ph-x" />
          </button>
        </header>

        <label className="wb-field">
          <span>Provider</span>
          <select
            className="v-input"
            value={kind}
            disabled={!!provider}
            onChange={(event) => setKind(event.target.value)}
          >
            {(provider ? directory.catalog.filter((item) => item.name === kind) : offers).map((item) => (
              <option key={item.name} value={item.name}>
                {item.label}
              </option>
            ))}
            <option value={OTHER}>Other (OpenAI-compatible)</option>
          </select>
        </label>

        {custom && (
          <>
            <label className="wb-field">
              <span>Name</span>
              <input
                className="v-input"
                value={label}
                disabled={!!provider}
                placeholder="e.g. DeepSeek, Groq, Together"
                onChange={(event) => setLabel(event.target.value)}
              />
            </label>
            <label className="wb-field">
              <span>Base URL</span>
              <input
                className="v-input mono"
                value={baseUrl}
                placeholder="https://api.example.com/v1"
                onChange={(event) => setBaseUrl(event.target.value)}
              />
            </label>
          </>
        )}

        <label className="wb-field">
          <span>API key</span>
          <input
            className="v-input mono"
            type="password"
            autoComplete="off"
            value={apiKey}
            placeholder={keyStored ? 'Stored — leave empty to keep it' : 'Paste your key'}
            onChange={(event) => setApiKey(event.target.value)}
          />
          <small>
            Saved to <code>~/.hiveloom/.env</code> on this machine, readable only by you. It is never
            shown again or written into a harness.
          </small>
        </label>

        <div className="wb-field">
          <span>Models in the composer</span>
          {catalogModels ? (
            <div className="wb-model-list">
              {offer!.choices.map((model) => (
                <label key={model.id} className="wb-model">
                  <input
                    type="checkbox"
                    checked={picked.includes(model.id)}
                    onChange={() =>
                      setPicked(
                        picked.includes(model.id)
                          ? picked.filter((item) => item !== model.id)
                          : [...picked, model.id],
                      )
                    }
                  />
                  <span className="mono">{model.id}</span>
                  {model.input_cost_per_mtok !== null && (
                    <em className="mono">
                      ${model.input_cost_per_mtok} / ${model.output_cost_per_mtok}
                    </em>
                  )}
                </label>
              ))}
            </div>
          ) : (
            <>
              <div className="wb-id-entry">
                <input
                  className="v-input mono"
                  value={draftId}
                  placeholder={kind === 'openrouter' ? 'e.g. deepseek/deepseek-v4-flash' : 'model id'}
                  onChange={(event) => setDraftId(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter') {
                      event.preventDefault()
                      addId()
                    }
                  }}
                />
                <button className="v-btn v-btn-ghost v-btn-sm" onClick={addId} disabled={!draftId.trim()}>
                  Add
                </button>
              </div>
              <div className="wb-chips">
                {picked.map((id) => (
                  <span key={id} className="wb-chip mono">
                    {id}
                    <button onClick={() => setPicked(picked.filter((item) => item !== id))} title={`Remove ${id}`}>
                      <i className="ph ph-x" />
                    </button>
                  </span>
                ))}
                {!picked.length && <small>Name the models you want to use — they appear in the composer.</small>}
              </div>
            </>
          )}
        </div>

        {error && <Notice icon="ph-warning-octagon" tone="err" title="Not saved" body={error} />}

        <footer>
          <button className="v-btn v-btn-ghost" onClick={onClose}>
            Cancel
          </button>
          <button className="v-btn v-btn-primary" disabled={busy || !canSave} onClick={() => void save()}>
            {busy && <i className="ph ph-circle-notch spin" />}
            {provider ? 'Save' : 'Add provider'}
          </button>
        </footer>
      </div>
    </div>
  )
}

export function Memory({ harness }: { harness: { id: string; name: string } | null }) {
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

