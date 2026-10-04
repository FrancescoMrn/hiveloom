/**
 * One harness's own settings: the model its spec names, and its memory.
 *
 * Unlike workbench Settings, everything here is part of what the harness *is*
 * and travels with it. The model is chosen from the providers this workbench
 * is set up with; when the spec names one that is not set up here, this says
 * what a run will use instead (the workbench default) rather than pretending.
 */
import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import type { HarnessDetail, Provider } from '../types'
import { canRun, useWorkbenchDirectory } from '../workbench'
import { Notice } from './common'
import { ModelPicker } from './ModelPicker'
import { Memory } from './Settings'

type SpecModel = {
  model?: { provider?: string; id?: string; temperature?: number | null }
  context?: { max_input_tokens?: number }
}

export function HarnessSettings({
  harness,
  onSaved,
}: {
  harness: HarnessDetail
  onSaved: () => Promise<void>
}) {
  const directory = useWorkbenchDirectory()
  const spec = harness.spec as SpecModel | undefined
  const specSelector = spec?.model?.provider && spec.model.id ? `${spec.model.provider}/${spec.model.id}` : ''

  const [selector, setSelector] = useState(specSelector)
  const [temperature, setTemperature] = useState(
    spec?.model?.temperature === null || spec?.model?.temperature === undefined
      ? ''
      : String(spec.model.temperature),
  )
  const [maxInput, setMaxInput] = useState(String(spec?.context?.max_input_tokens ?? ''))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState<string | null>(null)
  // Only to learn whether the spec's provider needs a key at all: an offline
  // provider a harness's own extension registers runs without one.
  const [registry, setRegistry] = useState<Provider[] | null>(null)

  useEffect(() => {
    setSelector(specSelector)
  }, [specSelector])

  useEffect(() => {
    let live = true
    api
      .providers(harness.id)
      .then((rows) => live && setRegistry(rows))
      .catch(() => live && setRegistry([]))
    return () => {
      live = false
    }
  }, [harness.id])

  const specProvider = registry?.find((row) => row.name === spec?.model?.provider)
  const keyless = specProvider ? !specProvider.api_key_env : false
  const runsAsWritten = keyless || canRun(directory, specSelector)
  const providerLabel = useMemo(() => {
    const name = spec?.model?.provider ?? ''
    return directory?.catalog.find((offer) => offer.name === name)?.label ?? specProvider?.label ?? name
  }, [directory, spec?.model?.provider, specProvider?.label])

  const save = async () => {
    setBusy(true)
    setError(null)
    try {
      const result = await api.setModel(harness.id, {
        selector,
        temperature: temperature.trim() === '' ? '' : Number(temperature),
        max_input_tokens: Number(maxInput),
      })
      await onSaved()
      setSaved(
        `Runs from now on use ${result.provider}/${result.id}. Runs already recorded keep the model they ran with.`,
      )
    } catch (exc) {
      setError(String(exc))
    } finally {
      setBusy(false)
    }
  }

  const dirty =
    selector !== specSelector ||
    temperature !== (spec?.model?.temperature == null ? '' : String(spec.model.temperature)) ||
    maxInput !== String(spec?.context?.max_input_tokens ?? '')

  return (
    <div className="harness-settings">
      <section className="settings-card">
        <div className="settings-card-head">
          <i className="ph ph-cube" style={{ color: 'var(--evo)' }} />
          <div className="settings-name">Run model</div>
          <span className="mono settings-path">{specSelector}</span>
        </div>
        <p className="settings-help">
          The model this harness's spec names — every turn of every run, unless a run switches.
        </p>

        {directory && registry && !runsAsWritten && (
          <div style={{ marginTop: 10 }}>
            <Notice
              icon="ph-arrow-bend-down-right"
              tone="warn"
              title={
                directory.effective_default
                  ? `Runs here use ${directory.effective_default} for now`
                  : 'This harness cannot run here yet'
              }
              body={
                directory.effective_default
                  ? `${providerLabel} is not set up on this workbench, so runs fall back to the default model. The spec keeps ${specSelector}; add ${providerLabel} in Settings → Models and runs use it again.`
                  : `${providerLabel} is not set up, and no other provider is either. Add one in Settings → Models.`
              }
            />
          </div>
        )}
        {keyless && (
          <p className="settings-help" style={{ marginTop: 10 }}>
            Runs on a model its own extension provides, offline — no key needed. Pick a hosted model
            below to run it for real.
          </p>
        )}

        <div style={{ marginTop: 12 }}>
          <ModelPicker
            directory={directory}
            value={selector}
            heldNote={keyless ? "offline, from this harness's extension" : undefined}
            heldBadge={keyless ? 'offline' : undefined}
            ariaLabel="Run model"
            onChange={(next) => {
              setSelector(next)
              setSaved(null)
            }}
          />
        </div>

        <div className="settings-grid" style={{ marginTop: 14 }}>
          <label className="wb-field">
            <span>Max input tokens per call</span>
            <input
              className="v-input mono"
              value={maxInput}
              inputMode="numeric"
              onChange={(event) => {
                setMaxInput(event.target.value)
                setSaved(null)
              }}
            />
          </label>
          <label className="wb-field">
            <span>Temperature</span>
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
          </label>
        </div>

        {error && <Notice icon="ph-warning-octagon" tone="err" title="Refused — the spec on disk is unchanged" body={error} />}
        {saved && <Notice icon="ph-check-circle" tone="ok" title="Saved and revalidated" body={saved} />}

        <div className="settings-actions">
          <span className="settings-help">
            Written through the validated construction API, as <code>hiveloom set model</code> would.
          </span>
          <button className="v-btn v-btn-primary" disabled={busy || !dirty || !selector} onClick={() => void save()}>
            {busy ? <i className="ph ph-circle-notch spin" /> : <i className="ph ph-check" />}
            Save
          </button>
        </div>
      </section>

      <div className="settings-card">
        <div className="settings-name" style={{ marginBottom: 6 }}>Memory</div>
        <Memory harness={{ id: harness.id, name: harness.name }} />
      </div>
    </div>
  )
}
