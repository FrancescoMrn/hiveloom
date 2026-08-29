import { useEffect, useState } from 'react'
import { api } from '../api'
import type { Catalog, HarnessDetail } from '../types'
import { Label, Notice } from './common'

/**
 * The spec editor, with the catalog beside it.
 *
 * The catalog panel is not decoration: a harness spec may only name tools,
 * validators, and guardrails that the loader knows, and the catalog is the
 * single source of that truth (builtins plus registered extensions). Showing it
 * here means the editor never carries its own copy of the list, which would
 * drift the moment an extension is installed.
 *
 * Saving validates before it commits, and the server restores the previous file
 * if validation fails — so a bad edit costs an error message, never a harness
 * that no longer loads.
 *
 * Neither column scrolls the screen. The editor stretches to the window and the
 * catalog scrolls under its own filter, because they are two unrelated lists
 * and a single scrollbar for both meant reading the end of the catalog took the
 * file you were editing off the top of the window.
 */
export function SpecEditor({
  harness,
  onSaved,
}: {
  harness: HarnessDetail
  onSaved: () => Promise<void>
}) {
  const [text, setText] = useState(harness.yaml)
  const [catalog, setCatalog] = useState<Catalog | null>(null)
  const [filter, setFilter] = useState('')
  const [status, setStatus] = useState<{ ok: boolean; message: string } | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    setText(harness.yaml)
    setStatus(null)
  }, [harness.id, harness.yaml])

  useEffect(() => {
    void api
      .catalog()
      .then(setCatalog)
      .catch(() => setCatalog(null))
  }, [])

  const dirty = text !== harness.yaml

  const act = async (what: 'validate' | 'save') => {
    setBusy(true)
    setStatus(null)
    try {
      if (what === 'save') {
        await api.saveSpec(harness.id, text)
        await onSaved()
        setStatus({ ok: true, message: 'Saved and validated.' })
      } else {
        const result = await api.validate(harness.id)
        setStatus({ ok: true, message: `Valid — ${result.name}` })
      }
    } catch (exc) {
      setStatus({ ok: false, message: String(exc) })
    } finally {
      setBusy(false)
    }
  }

  const kinds = Object.entries(catalog ?? {})
  const needle = filter.trim().toLowerCase()

  return (
    <div className="pane split">
      <div className="spec-edit">
        <div style={{ display: 'flex', alignItems: 'center', gap: 9, marginBottom: 14 }}>
          <button
            className="v-btn v-btn-primary v-btn-sm"
            disabled={!dirty || busy}
            onClick={() => void act('save')}
          >
            <i className="ph ph-floppy-disk" />
            Save
          </button>
          <button
            className="v-btn v-btn-ghost v-btn-sm"
            disabled={busy}
            onClick={() => void act('validate')}
          >
            <i className="ph ph-check-circle" />
            Validate on disk
          </button>
          {dirty && (
            <span className="v-label" style={{ color: 'var(--warn)' }}>
              unsaved
            </span>
          )}
          <span
            className="mono"
            style={{
              marginLeft: 'auto',
              fontSize: 11.5,
              color: 'var(--mut)',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}
          >
            {harness.yaml_path}
          </span>
        </div>

        {status && (
          <div style={{ marginBottom: 14 }}>
            <Notice
              icon={status.ok ? 'ph-check-circle' : 'ph-warning-octagon'}
              tone={status.ok ? 'ok' : 'err'}
              title={status.ok ? 'OK' : 'Refused — the file on disk is unchanged'}
              body={status.message}
            />
          </div>
        )}

        {/* No height of its own: the column is as tall as the window, and the
            file takes whatever the toolbar and any notice above it leave. */}
        <textarea
          className="v-input"
          value={text}
          spellCheck={false}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 's' && (e.metaKey || e.ctrlKey)) {
              e.preventDefault()
              if (dirty) void act('save')
            }
          }}
        />
      </div>

      {/* The filter is pinned and the list moves under it: the catalog is
          longer than any window, and a filter you have to scroll back up to
          reach is a filter you stop using. */}
      <div className="v-panel catalog-panel">
        <header>
          <Label>Catalog</Label>
          <input
            className="v-input"
            placeholder="Filter…"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
        </header>
        <div className="catalog-scroll">
          {catalog === null ? (
            <div style={{ color: 'var(--mut)', fontSize: 13 }}>unavailable</div>
          ) : (
            kinds.map(([kind, entries]) => {
              const shown = entries.filter(
                (e) =>
                  !needle ||
                  e.name.toLowerCase().includes(needle) ||
                  e.description.toLowerCase().includes(needle),
              )
              if (shown.length === 0) return null
              return (
                <div key={kind} style={{ marginBottom: 16 }}>
                  <Label>{kind}</Label>
                  <div className="catalog-list">
                    {shown.map((entry) => (
                      <div
                        key={entry.name}
                        className="v-row catalog-row"
                        title={`${entry.name} — ${entry.description}`}
                      >
                        <div className="mono catalog-name">{entry.name}</div>
                        <div className="catalog-desc">{entry.description}</div>
                      </div>
                    ))}
                  </div>
                </div>
              )
            })
          )}
        </div>
      </div>
    </div>
  )
}
